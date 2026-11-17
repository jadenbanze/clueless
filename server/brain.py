"""Answer generation: transcript + retrieved context -> a short reply.

Two tiers, because their requirements differ:

  live    - triggered by a hotkey mid-sentence. Must be fast and terse, and is
            allowed to say it does not know.
  summary - runs over the full transcript, optimised for completeness.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import config, llm, profiles, providers, rag

log = logging.getLogger(__name__)

LIVE_SYSTEM = """You are a meeting assistant answering one participant's question.

TRANSCRIPT is reliable and verbatim: answer from it first. "me" and "them" are
transcription labels, not people.
REFERENCE material is retrieved by similarity and is often irrelevant: use it
only when it directly answers the question.

Rules:
- 1-3 sentences. Decisive, no preamble, no headings, no bullet lists unless asked.
- Give the substance, never a restatement of the question. Always state both the
  subject (which decision, blocker, concept, person or system) AND its current
  status (e.g. what is blocking, pending or still needs sign-off).
- Never hedge ("I think", "it seems", "possibly"). State the answer.
- Never begin with "From the transcript" or quote speaker labels.
- Never invent ticket IDs, citations, dosages, names, dates, URLs or behaviour.
- Never guess who is presenting or attending unless listed in Meeting calendar;
  otherwise say "I can't identify participants from audio."
- If neither source answers, reply exactly: "No info in the loaded context."
- The transcript may contain speech-recognition errors, filler and missing
  punctuation; answer what was meant.
- If a "What is on the user's screen" section appears, it is OCR of their screen:
  use it for references like "this" or "that slide"; ignore UI chrome.
"""

INTENT_MAX_TOKENS = {"answer": 90, "suggest": 100, "explain": 140}

INTENTS = {
    "answer": (LIVE_SYSTEM, None),
    "suggest": (
        """You are helping one participant decide what to say next in a live meeting or discussion.

Given the recent transcript, draft the reply THEY should give. Write it in the
first person, as they would say it aloud.

Rules:
- 1-2 sentences. Natural spoken language, not written prose.
- Commit to a position grounded in the transcript or reference material. If
  something is unknown, say what they would need to check rather than inventing it.
- Never invent names, dates, ticket IDs, citations or commitments.
- No preamble, no quotation marks, no "you could say".
""",
        "Draft my reply to the last thing said.",
    ),
    "explain": (
        """You explain what the user is currently looking at or hearing.

You get OCR of their screen (when available), the recent transcript, and any
matching reference notes. Explain the artefact — a slide, concept, formula,
ticket, error, diagram or case detail — in terms of what it means here.

Rules:
- 1-3 sentences. Concrete, no preamble.
- Speak directly about the artefact. Never write "the user", "you are looking
  at", or "the screen shows" — just say what it is and what matters about it.
- Ground every claim in the screen text, transcript or references. If the screen
  text is unclear or empty, explain the most recent technical concept in the
  transcript or say plainly what is missing.
- Ignore OCR artefacts and application chrome.
- Never invent numbers, dosages, citations or names that are not present.
""",
        "What am I looking at, and why does it matter here?",
    ),
}

SUMMARY_SYSTEM = profiles.SUMMARY_PROMPTS["standard"]

_DEICTIC_WORDS = {
    "this", "that", "these", "those", "it", "its", "they", "them", "their",
    "there", "here", "blocker", "issue", "ticket", "problem", "slide", "error",
    "concept", "drug", "study", "paper", "trial", "change", "pr",
}


@dataclass
class Answer:
    text: str
    citations: list[str]
    model: str
    elapsed_ms: int
    citation_links: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "citations": self.citations,
            "citation_links": self.citation_links,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
        }


ABSTENTION = "No info in the loaded context."

_HEDGE_PREFIXES = (
    "i think ", "i believe ", "it seems ", "it appears ", "possibly ",
    "perhaps ", "maybe ", "it might be ", "it could be ", "in my opinion ",
)


def strip_hedging(text: str) -> str:
    stripped = text.lstrip()
    lowered = stripped.lower()
    for prefix in _HEDGE_PREFIXES:
        if lowered.startswith(prefix):
            remainder = stripped[len(prefix):]
            if len(remainder.strip(" .\n")) > 8:
                return remainder[0].upper() + remainder[1:] if remainder else remainder
    return text


def clean_answer(text: str) -> str:
    text = text.strip()
    if not text:
        return text

    lines = [line for line in text.split("\n") if not line.strip().startswith("#")]
    text = "\n".join(lines).strip()
    text = strip_hedging(text)

    lowered = text.lower()
    if ABSTENTION.lower() in lowered:
        pieces = [
            piece.strip()
            for piece in text.replace(ABSTENTION, "\x00").split("\x00")
            if piece.strip(" .\n")
        ]
        substantive = [p for p in pieces if p.strip(" .\n")]
        if substantive:
            return " ".join(substantive).strip()
        return ABSTENTION

    return text


def _user_context_line() -> str:
    if config.CLINICAL_MODE:
        return ""
    parts = [p.strip() for p in (config.USER_NAME, config.USER_ROLE, config.USER_FOCUS) if p and p.strip()]
    if not parts:
        return ""
    return f"Participant profile (for tone/role only, never invent facts): {' · '.join(parts)[:220]}"


def _retrieval_query(question: str, transcript_text: str) -> str:
    """Builds the query string for retrieval, resolving deictic references."""
    question = question.strip()
    words = [w.strip(",.?!:;\"'()").lower() for w in question.split() if w.strip(",.?!:;\"'()")]
    has_deictic = any(w in _DEICTIC_WORDS for w in words)
    has_specific = bool(re.search(r"\b([A-Z]{2,}[A-Z0-9_-]*|[A-Z][a-z]+[A-Z]\w*)\b", question))

    if len(words) >= 4 and (has_specific or not has_deictic):
        return question

    # Pull salient terms and identifiers from the last 250 chars of transcript
    # rather than dumping raw conversational filler that dilutes embeddings.
    tail = transcript_text[-280:]
    salient = re.findall(r"\b(?:[A-Z]{2,}[A-Z0-9_-]*|[A-Za-z][A-Za-z0-9_-]{4,})\b", tail)
    filtered = [
        tok for tok in salient
        if tok.lower() not in {"there", "their", "about", "which", "would", "could", "should", "still", "needs"}
    ]
    extra = " ".join(dict.fromkeys(filtered[-10:]))
    return f"{question} {extra}".strip() if extra else f"{question}\n{transcript_text[-200:]}".strip()


def _build_prompt(
    question: str,
    transcript_text: str,
    chunks: list[rag.Chunk],
    *,
    intent: str,
    screen_text: str | None = None,
    calendar_text: str | None = None,
) -> tuple[str, str, str]:
    transcript = transcript_text.strip()
    if len(transcript) > config.TRANSCRIPT_MAX_CHARS:
        transcript = "…\n" + transcript[-config.TRANSCRIPT_MAX_CHARS:]

    system_prompt, default_question = INTENTS.get(intent, INTENTS["answer"])
    system_prompt = profiles.apply_addendum(
        system_prompt, config.PROFILE, clinical=config.CLINICAL_MODE
    )
    user_line = _user_context_line()
    if user_line:
        system_prompt = f"{system_prompt.rstrip()}\n- {user_line}\n"

    resolved_question = (question or default_question or "").strip()

    sections = [
        f"### Question\n{resolved_question}",
        f"### Recent transcript\n{transcript or '(empty)'}",
    ]
    if calendar_text and not config.CLINICAL_MODE:
        sections.append(f"### Meeting calendar\n{calendar_text.strip()[:450]}")
    if screen_text:
        sections.append(f"### What is on the user's screen\n{screen_text.strip()[:900]}")
    sections.append(f"### Reference material\n{rag.format_context(chunks, query=resolved_question)}")
    sections.append("Answer the question in the required style.")
    return system_prompt, "\n\n".join(sections), resolved_question


def _should_retrieve(intent: str, question: str, transcript_text: str, sources: set[str] | None) -> bool:
    if sources is not None and not (sources & set(config.SOURCE_COLLECTIONS)):
        return False
    # For "What should I say?", skip RAG unless a specific identifier or technical
    # question appears, saving ~80ms and avoiding irrelevant context injection.
    if intent == "suggest" and not question:
        tail = transcript_text[-220:]
        if "?" not in tail and not rag.extract_identifiers(tail):
            return False
    return True


def answer_question(
    question: str,
    transcript_text: str,
    store: rag.Store | None = None,
    sources: set[str] | None = None,
    screen_text: str | None = None,
    calendar_text: str | None = None,
    intent: str = "answer",
) -> Answer:
    started = time.monotonic()

    retrieval_started = time.monotonic()
    chunks: list[rag.Chunk] = []
    if store is not None and _should_retrieve(intent, question, transcript_text, sources):
        seed = question or (INTENTS.get(intent, INTENTS["answer"])[1] or "")
        chunks = store.search_all(_retrieval_query(seed, transcript_text), sources=sources)
    retrieval_ms = int((time.monotonic() - retrieval_started) * 1000)

    system_prompt, prompt, _resolved_q = _build_prompt(
        question, transcript_text, chunks,
        intent=intent, screen_text=screen_text, calendar_text=calendar_text,
    )

    generation_started = time.monotonic()
    text = llm.complete(
        system_prompt, prompt,
        max_tokens=INTENT_MAX_TOKENS.get(intent, config.LIVE_NUM_PREDICT),
        temperature=config.LIVE_TEMPERATURE,
        task="live",
    )
    generation_ms = int((time.monotonic() - generation_started) * 1000)
    t = providers.last_timings
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
        citation_links=[chunk.citation_meta() for chunk in chunks],
        model=config.LIVE_MODEL,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def answer_question_streaming(
    question: str,
    transcript_text: str,
    store: rag.Store | None = None,
    on_delta: Callable[[str], None] | None = None,
    sources: set[str] | None = None,
    screen_text: str | None = None,
    calendar_text: str | None = None,
    intent: str = "answer",
) -> Answer:
    started = time.monotonic()

    chunks: list[rag.Chunk] = []
    if store is not None and _should_retrieve(intent, question, transcript_text, sources):
        seed = question or (INTENTS.get(intent, INTENTS["answer"])[1] or "")
        chunks = store.search_all(_retrieval_query(seed, transcript_text), sources=sources)

    system_prompt, prompt, _resolved_q = _build_prompt(
        question, transcript_text, chunks,
        intent=intent, screen_text=screen_text, calendar_text=calendar_text,
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
        citation_links=[chunk.citation_meta() for chunk in chunks],
        model=config.LIVE_MODEL,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def summarise(transcript_text: str) -> Answer:
    started = time.monotonic()
    text = llm.complete(
        profiles.summary_prompt(config.PROFILE, clinical=config.CLINICAL_MODE),
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


def summarise_streaming(
    transcript_text: str, on_delta: Callable[[str], None] | None = None
) -> Answer:
    started = time.monotonic()
    parts: list[str] = []
    for piece in llm.stream(
        profiles.summary_prompt(config.PROFILE, clinical=config.CLINICAL_MODE),
        f"### Transcript\n{transcript_text.strip() or '(empty)'}",
        max_tokens=config.SUMMARY_NUM_PREDICT,
        temperature=0.1,
        task="summary",
    ):
        parts.append(piece)
        if on_delta:
            on_delta(piece)
    return Answer(
        text="".join(parts).strip() or "No summary produced.",
        citations=[],
        model=config.SUMMARY_MODEL,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
