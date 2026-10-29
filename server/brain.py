"""Answer generation: transcript + retrieved context -> a short reply.

Two tiers, because their requirements differ:

  live    - triggered by a hotkey mid-sentence. Must be fast and terse, and is
            allowed to say it does not know.
  summary - runs once at the end. Given the whole transcript, optimised for
            completeness rather than latency.

Model choice is constrained by what is actually on disk: the corporate network
blocks registry.ollama.ai, so nothing new can be pulled. In testing llama3.2 and
llama3.1:8b correctly abstained when context was missing, while qwen2.5
confabulated a confident false status — disqualifying for a RAG tool that reports
on real tickets.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import config, llm, ollama_client, rag

log = logging.getLogger(__name__)

LIVE_SYSTEM = """You are a meeting assistant answering one participant's question.

TRANSCRIPT is reliable and verbatim: answer from it first. "me" and "them" are
transcription labels, not people.
REFERENCE material is retrieved by similarity and is often irrelevant: use it
only when it directly answers the question.

Rules:
- 1-3 sentences. Decisive, no preamble, no headings, no bullet lists unless asked.
- Give the substance, never a restatement of the question. If the transcript says
  "still needs sign-off", the answer is that it is pending sign-off.
- Never hedge ("I think", "it seems", "possibly"). State the answer.
- Never begin with "From the transcript" or quote speaker labels.
- Never invent ticket IDs, names, dates, URLs or code behaviour.
- Never guess who is presenting or attending; say "I can't identify participants
  from audio."
- If neither source answers, reply exactly: "No info in the loaded context."
- The transcript may contain speech-recognition errors, filler and missing
  punctuation; answer what was meant.
- If a "What is on the user's screen" section appears, it is OCR of their screen:
  use it for references like "this" or "that slide"; ignore UI chrome.
"""

# Intent variants. Every mature Cluely-style tool has distinct actions rather
# than one generic "ask" — the prompt can then be specific, which costs nothing
# at runtime and improves answers more than any remaining latency work.
#
# Each entry is (instructions, question-template). The template receives the
# transcript's most recent turn where relevant.
INTENT_MAX_TOKENS = {"answer": 90, "suggest": 100, "explain": 140}

INTENTS = {
    "answer": (LIVE_SYSTEM, None),
    "suggest": (
        """You are helping one participant decide what to say next in a live meeting.

Given the recent transcript, draft the reply THEY should give. Write it in the
first person, as they would say it aloud.

Rules:
- 1-2 sentences. Natural spoken language, not written prose.
- Commit to a position. If something is unknown, say what they would need to
  check rather than inventing it.
- Never invent names, dates, ticket IDs or commitments.
- No preamble, no quotation marks, no "you could say".
""",
        "Draft my reply to the last thing said.",
    ),
    "explain": (
        """You explain what the user is currently looking at.

You get OCR of their screen and the recent transcript. Explain the artefact —
a slide, a ticket, an error, a diagram — in terms of what it means for the
discussion.

Rules:
- 1-3 sentences. Concrete, no preamble.
- Speak directly about the artefact. Never write "the user", "you are looking
  at", or "the screen shows" — just say what it is and what matters about it.
- Ground every claim in the screen text or transcript. If the screen text is
  unclear or empty, say so plainly.
- Ignore OCR artefacts and application chrome.
- Never invent numbers or names that are not present.
""",
        "What am I looking at, and why does it matter here?",
    ),
}


SUMMARY_SYSTEM = """You summarise a work meeting from its transcript.

Produce markdown with exactly these sections:

## Summary
2-4 sentences on what the meeting was about.

## Decisions
Bullets. Only decisions actually made. Write "None recorded" if there are none.

## Action items
Bullets formatted "owner — action". The owner must be a person's name that was
actually said aloud. If no name was given, write "Unassigned — action".
Never use the transcript speaker labels "me" or "them" as an owner; they are
transcription artefacts, not names.

## Open questions
Bullets. Unresolved issues or things someone needs to follow up.

Rules:
- Only use what is in the transcript. Do not infer facts that were not said.
- If a section has nothing, write "None recorded".
- Attribute statements to speakers using the transcript labels (me/them).
"""


@dataclass
class Answer:
    text: str
    citations: list[str]
    model: str
    elapsed_ms: int

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "citations": self.citations,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
        }


ABSTENTION = "No info in the loaded context."

# Openers that weaken an answer. The prompt forbids them, but a small model still
# produces them, and in a meeting a hedged answer reads as uncertainty the model
# does not actually have.
_HEDGE_PREFIXES = (
    "i think ", "i believe ", "it seems ", "it appears ", "possibly ",
    "perhaps ", "maybe ", "it might be ", "it could be ", "in my opinion ",
)


def strip_hedging(text: str) -> str:
    """Removes a leading hedge so the answer starts with its actual content."""
    stripped = text.lstrip()
    lowered = stripped.lower()
    for prefix in _HEDGE_PREFIXES:
        if lowered.startswith(prefix):
            remainder = stripped[len(prefix):]
            # Only strip when something substantive follows.
            if len(remainder.strip(" .\n")) > 8:
                return remainder[0].upper() + remainder[1:] if remainder else remainder
    return text


def clean_answer(text: str) -> str:
    """Removes structural artifacts the small model emits.

    llama3.2 tends to append the abstention sentence even when it has already
    answered — e.g. "We are blocked on Kafka. No info in the loaded context."
    — and to add "### Answer" headings despite being told not to. Both are
    formatting faults rather than reasoning faults, so they are corrected here
    instead of by escalating to a larger, much slower model.
    """
    text = text.strip()
    if not text:
        return text

    # Drop markdown headings the model adds unprompted.
    lines = [line for line in text.split("\n") if not line.strip().startswith("#")]
    text = "\n".join(lines).strip()
    text = strip_hedging(text)

    lowered = text.lower()
    if ABSTENTION.lower() in lowered:
        # Keep only the parts that are not the abstention sentence.
        pieces = [
            piece.strip()
            for piece in text.replace(ABSTENTION, "\x00").split("\x00")
            if piece.strip(" .\n")
        ]
        substantive = [p for p in pieces if p.strip(" .\n")]
        if substantive:
            # There is a real answer; the abstention was spurious.
            return " ".join(substantive).strip()
        return ABSTENTION

    return text


def answer_question(
    question: str, transcript_text: str, store: rag.Store | None = None,
    sources: set[str] | None = None, screen_text: str | None = None,
    intent: str = "answer",
) -> Answer:
    started = time.monotonic()

    retrieval_started = time.monotonic()
    chunks: list[rag.Chunk] = []
    if store is not None:
        chunks = store.search_all(_retrieval_query(question, transcript_text), sources=sources)
    retrieval_ms = int((time.monotonic() - retrieval_started) * 1000)

    transcript = transcript_text.strip()
    if len(transcript) > config.TRANSCRIPT_MAX_CHARS:
        # Keep the tail: the most recent exchange is what is being discussed.
        transcript = "…\n" + transcript[-config.TRANSCRIPT_MAX_CHARS:]

    # The question comes first, then the transcript. With the question last, the
    # model latched onto whichever exchange happened to sit nearest the end —
    # asking "what is blocking the release?" returned the answer to the *final*
    # utterance about read-only access instead.
    system_prompt, default_question = INTENTS.get(intent, INTENTS["answer"])
    question = question or (default_question or "")

    prompt = (
        f"### Question\n{question.strip()}\n\n"
        f"### Recent transcript\n{transcript or '(empty)'}\n\n"
        + (f"### What is on the user's screen\n{screen_text.strip()[:900]}\n\n" if screen_text else "")
        + f"### Reference material\n{rag.format_context(chunks, query=question)}\n\n"
        "Answer the question in the required style."
    )

    generation_started = time.monotonic()
    text = llm.complete(
        system_prompt, prompt,
        max_tokens=INTENT_MAX_TOKENS.get(intent, config.LIVE_NUM_PREDICT),
        temperature=config.LIVE_TEMPERATURE,
        task="live",
    )
    generation_ms = int((time.monotonic() - generation_started) * 1000)
    t = ollama_client.last_timings
    log.info(
        "answer: %d chars, retrieval %dms, generation %dms, %d chunks | "
        "prompt %s tok in %sms (%s tok/s), out %s tok in %sms (%s tok/s), load %sms",
        len(prompt), retrieval_ms, generation_ms, len(chunks),
        t.get("prompt_tokens"), t.get("prompt_ms"), t.get("prompt_tok_per_s"),
        t.get("output_tokens"), t.get("output_ms"), t.get("output_tok_per_s"),
        t.get("load_ms"),
    )

    return Answer(
        text=clean_answer(text) or "No answer produced.",
        citations=[chunk.citation() for chunk in chunks],
        model=config.LIVE_MODEL,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _retrieval_query(question: str, transcript_text: str) -> str:
    """Builds the text used for retrieval.

    A self-contained question is used on its own. Appending transcript context
    dilutes it badly: asking "is there a Jira ticket about Kafka authentication?"
    during a meeting about Snyk scanning retrieved Playwright tickets, because
    600 characters of unrelated transcript outweighed the three meaningful words
    in the question.

    Transcript context is only added when the question is too vague to search on
    its own ("what about that?", "why?").
    """
    question = question.strip()
    if len(question.split()) >= 4:
        return question
    return f"{question}\n{transcript_text[-200:]}".strip()


def answer_question_streaming(
    question: str,
    transcript_text: str,
    store: rag.Store | None = None,
    on_delta: Callable[[str], None] | None = None,
    sources: set[str] | None = None,
    screen_text: str | None = None,
    intent: str = "answer",
) -> Answer:
    """Same as answer_question, but emits fragments as they are generated.

    Returns the final, cleaned answer. Callers use `on_delta` to paint the
    overlay while generation is still running; the returned text is authoritative
    because cleaning can only be applied once the whole answer exists.
    """
    started = time.monotonic()

    chunks: list[rag.Chunk] = []
    if store is not None:
        chunks = store.search_all(_retrieval_query(question, transcript_text), sources=sources)

    transcript = transcript_text.strip()
    if len(transcript) > config.TRANSCRIPT_MAX_CHARS:
        transcript = "…\n" + transcript[-config.TRANSCRIPT_MAX_CHARS:]

    system_prompt, default_question = INTENTS.get(intent, INTENTS["answer"])
    question = question or (default_question or "")

    prompt = (
        f"### Question\n{question.strip()}\n\n"
        f"### Recent transcript\n{transcript or '(empty)'}\n\n"
        + (f"### What is on the user's screen\n{screen_text.strip()[:900]}\n\n" if screen_text else "")
        + f"### Reference material\n{rag.format_context(chunks, query=question)}\n\n"
        "Answer the question in the required style."
    )

    parts: list[str] = []
    for piece in llm.stream(
        system_prompt, prompt,
        max_tokens=INTENT_MAX_TOKENS.get(intent, config.LIVE_NUM_PREDICT),
        temperature=config.LIVE_TEMPERATURE,
    ):
        parts.append(piece)
        if on_delta:
            on_delta(piece)

    raw = "".join(parts)
    return Answer(
        text=clean_answer(raw) or "No answer produced.",
        citations=[chunk.citation() for chunk in chunks],
        model=config.LIVE_MODEL,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def summarise(transcript_text: str) -> Answer:
    started = time.monotonic()
    text = llm.complete(
        SUMMARY_SYSTEM,
        f"### Transcript\n{transcript_text.strip() or '(empty)'}",
        max_tokens=config.SUMMARY_NUM_PREDICT,
        temperature=0.1,
    )
    return Answer(
        text=text or "No summary produced.",
        citations=[],
        model=config.SUMMARY_MODEL,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
