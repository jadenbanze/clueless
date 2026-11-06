"""Regression tests for answer generation.

Each case encodes a failure that actually occurred during development, so it
cannot silently come back:

  * confabulated troubleshooting steps from irrelevant code  (15.4s, invented
    "[insert correlation ID]" placeholders)
  * abstaining on a question the transcript plainly answered
  * answering the wrong transcript line (question placed last in the prompt)
  * leaking the few-shot example's content into the answer
  * emitting "No info" alongside a real answer
  * inventing a ticket ID ("PROJ-#")

Run: python3 -m server.test_brain
"""

from __future__ import annotations

import logging
import sys

from . import brain, config, providers, rag

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("brain-test")

TRANSCRIPT = (
    "them: We are blocked on the Kafka authentication failure for the billing "
    "service.\n"
    "me: Priya is investigating the JWT processing error.\n"
    "them: Do we have a decision on read-only access before Friday?\n"
    "me: No, that still needs sign-off."
)

# (question, required_any, forbidden)
# required_any is a list of alternative groups: each group must match at least
# one member. Spelling varies between runs ("read-only" vs "read only"), so
# alternatives are expressed per group rather than as a flat list.
CASES = [
    ("What is blocking the release?", [["kafka"]], ["[insert", "correlation id"]),
    ("Who is investigating the JWT error?", [["priya"]], ["[insert"]),
    (
        "What decision is needed before Friday?",
        # "pending" is equally correct: the transcript says it still needs
        # sign-off, so either phrasing answers the question. Asserting only
        # "sign-off" rejected good answers from the Foundation Models backend.
        [["read-only", "read only"],
         ["sign-off", "sign off", "signoff", "approval", "pending", "outstanding"]],
        # The few-shot example leaked this into answers verbatim.
        ["legal", "security sign-off is still pending"],
    ),
    ("How do I bake sourdough bread?", [["no info"]], ["kafka"]),
    ("What is the status of the deployment?", [], []),
]

FORBIDDEN_ALWAYS = ["from the transcript", "them:", "me:", "proj-#", "[insert"]


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return condition


def main() -> int:
    failures = 0

    print("== environment ==")
    resolution = providers.resolve(refresh=True)
    failures += not check("model provider available", resolution.provider is not None, resolution.message)
    if resolution.provider is None:
        return 1
    failures += not check(
        f"live model in use ({config.LIVE_MODEL})", resolution.status == "ok",
        f"{resolution.provider.label} / {resolution.model}",
    )

    store = None
    try:
        store = rag.Store()
        print(f"  retrieval: {store.count(config.JIRA_COLLECTION)} jira, "
              f"{store.count(config.CODE_COLLECTION)} code chunks")
    except Exception as error:
        print(f"  retrieval unavailable ({error}); running transcript-only")

    print("\n== answers ==")
    latencies = []
    for question, required, forbidden in CASES:
        answer = brain.answer_question(question, TRANSCRIPT, store)
        latencies.append(answer.elapsed_ms)
        text = answer.text
        lowered = text.lower()

        print(f"  Q: {question}")
        print(f"  A ({answer.elapsed_ms}ms): {text[:160]}")

        for group in required:
            matched = next((option for option in group if option in lowered), None)
            failures += not check(
                f"    mentions one of {group}", matched is not None, str(matched)
            )
        for needle in forbidden + FORBIDDEN_ALWAYS:
            failures += not check(f"    avoids '{needle}'", needle not in lowered)
        if "no info" in lowered:
            failures += not check("    abstention is the whole answer", len(text) < 120, text[:80])
        print()

    average = sum(latencies) // len(latencies)
    failures += not check("average latency under 6s", average < 6000, f"{average}ms")

    print("\n== summary ==")
    summary = brain.summarise(TRANSCRIPT)
    print("  " + summary.text.replace("\n", "\n  ")[:500])
    lowered = summary.text.lower()
    # Each profile has its own summary shape (server/profiles.py SUMMARY_PROMPTS).
    sections = {
        "general": ("what happened", "decisions", "next steps"),
    }.get(config.PROFILE, ("summary", "decisions", "action items", "open questions"))
    for section in sections:
        failures += not check(f"  has '{section}'", section in lowered)
    failures += not check("  mentions Kafka", "kafka" in lowered)

    print("\n== clean_answer unit cases ==")
    unit_cases = [
        ("We are blocked on Kafka. No info in the loaded context.", "We are blocked on Kafka."),
        ("No info in the loaded context.", "No info in the loaded context."),
        ("### Answer\nPriya is on it.", "Priya is on it."),
        ("Priya is investigating.", "Priya is investigating."),
    ]
    for raw, expected in unit_cases:
        actual = brain.clean_answer(raw)
        failures += not check(f"  {raw[:44]!r} -> {expected!r}", actual == expected, actual)

    print(f"\n===== {'PASS' if failures == 0 else f'{failures} FAILURE(S)'} =====")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
