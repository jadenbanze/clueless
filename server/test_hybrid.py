"""Measures whether hybrid routing (fast model + fallback) beats the slow model.

The decision hinges on the fallback rate. If Foundation Models is trustworthy
most of the time, hybrid wins big. If it fails often, those questions pay the
fast model's latency AND the slow model's, which is worse than just being slow.

Run: python3 -m server.test_hybrid
"""

from __future__ import annotations

import logging
import time

from server import config, hybrid, llm, ollama_client

logging.basicConfig(level=logging.WARNING)

KAFKA = (
    "them: We are blocked on the Kafka authentication failure for the restaurant profile service.\n"
    "me: Shweta is investigating the JWT processing error.\n"
    "them: Do we have a decision on read-only access before Friday?\n"
    "me: No, that still needs sign-off."
)

SNYK = (
    "them: The objective is a more reliable process around vulnerability scanning.\n"
    "them: This diagram shows the architecture of how the Snyk scan agent integrates with our workflow.\n"
    "them: It executes the Snyk scans and generates a vulnerability report.\n"
    "them: The first step is to discover critical and high severity vulnerabilities.\n"
    "them: We are not going to analyse medium or low vulnerabilities.\n"
    "me: Who owns the remediation step?\n"
    "them: Nobody yet, it is unassigned.\n"
    "them: We decided to keep the agent read-only for now, it will not push changes."
)

# (transcript, question, keywords that must appear in a correct answer, must_abstain)
CASES = [
    (KAFKA, "What is blocking the release?", ["kafka"], False),
    (KAFKA, "Who is investigating the JWT error?", ["shweta"], False),
    (KAFKA, "What decision is needed before Friday?", ["read", "sign"], False),
    (KAFKA, "How do I bake sourdough bread?", [], True),
    (SNYK, "What is the first step in the agent workflow?", ["critical"], False),
    (SNYK, "Which vulnerabilities are out of scope?", ["medium", "low"], False),
    (SNYK, "Is the agent allowed to push changes?", ["read", "not", "no"], False),
    (SNYK, "Who owns the remediation step?", ["unassigned", "nobody", "no one"], False),
    (SNYK, "What is the marketing launch date?", [], True),
]

# Production prompt for both backends. An earlier version of this harness used a
# terse prompt for both, which handicapped the 8B model — it is tuned against the
# full instruction set and scored 4/9 on the short one while passing 5/5 in the
# real suite. Comparing models on a prompt neither uses is meaningless.
from server import brain

INSTRUCTIONS = brain.LIVE_SYSTEM


def build_prompt(question: str, transcript: str) -> str:
    return (
        f"### Question\n{question.strip()}\n\n"
        f"### Recent transcript\n{transcript.strip()}\n\n"
        f"### Reference material\n(no matching context found)\n\n"
        "Answer the question in the required style."
    )


def ask(backend: str, question: str, transcript: str) -> tuple[str, int]:
    prompt = build_prompt(question, transcript)
    started = time.monotonic()
    if backend == "foundation":
        text = "".join(llm._foundation_stream(INSTRUCTIONS, prompt, config.LIVE_NUM_PREDICT))
    else:
        text = ollama_client.chat(
            config.LIVE_MODEL, INSTRUCTIONS, prompt,
            num_ctx=config.LIVE_NUM_CTX, num_predict=config.LIVE_NUM_PREDICT,
            temperature=config.LIVE_TEMPERATURE,
        )
    return text.strip(), int((time.monotonic() - started) * 1000)


def is_correct(answer: str, keywords: list[str], must_abstain: bool) -> bool:
    lowered = answer.lower()
    if must_abstain:
        return "no info" in lowered
    if "no info" in lowered:
        return False
    return all(word in lowered for word in keywords)


def main() -> int:
    if llm.backend() != "foundation":
        print("Foundation Models unavailable — hybrid is not possible on this machine")
        return 1

    print("warming models…")
    ollama_client.warm()

    fm_correct = fm_total = fm_ms_total = 0
    fallbacks = 0
    hybrid_ms_total = hybrid_correct = 0
    slow_correct = slow_ms_total = 0
    reasons: list[str] = []

    print(f"\n{'question':<44} {'fast':>7} {'valid':>16} {'hybrid':>8} {'slow':>7}")
    print("-" * 88)

    for transcript, question, keywords, must_abstain in CASES:
        fast, fast_ms = ask("foundation", question, transcript)
        unreliable, reason = hybrid.answer_is_unreliable(fast, question, transcript)

        if unreliable:
            fallbacks += 1
            reasons.append(reason)
            slow, slow_ms = ask("ollama", question, transcript)
            chosen, total_ms = slow, fast_ms + slow_ms
        else:
            chosen, total_ms = fast, fast_ms

        fast_ok = is_correct(fast, keywords, must_abstain)
        hybrid_ok = is_correct(chosen, keywords, must_abstain)
        slow_only, slow_only_ms = ask("ollama", question, transcript)
        slow_ok = is_correct(slow_only, keywords, must_abstain)

        fm_total += 1
        fm_correct += fast_ok
        fm_ms_total += fast_ms
        hybrid_correct += hybrid_ok
        hybrid_ms_total += total_ms
        slow_correct += slow_ok
        slow_ms_total += slow_only_ms

        flag = "FALLBACK" if unreliable else "keep"
        print(f"{question[:44]:<44} {fast_ms:>6}ms {flag:>16} {total_ms:>7}ms {slow_only_ms:>6}ms")

    n = len(CASES)
    print("-" * 88)
    print(f"{'correct':<44} {fm_correct}/{n:>5} {'':>16} {hybrid_correct}/{n:>6} {slow_correct}/{n:>6}")
    print(f"{'avg latency':<44} {fm_ms_total // n:>6}ms {'':>16} {hybrid_ms_total // n:>7}ms {slow_ms_total // n:>6}ms")
    print(f"\nfallback rate: {fallbacks}/{n} ({100 * fallbacks // n}%)")
    for reason in reasons:
        print(f"  - {reason}")

    print("\nverdict:")
    if hybrid_correct < slow_correct:
        print("  hybrid is LESS correct than the slow model — do not ship")
        return 1
    if hybrid_ms_total < slow_ms_total:
        saved = slow_ms_total - hybrid_ms_total
        print(f"  hybrid saves {saved // n}ms per question at equal correctness — worth shipping")
        return 0
    print("  hybrid is slower AND no more correct — not worth it")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
