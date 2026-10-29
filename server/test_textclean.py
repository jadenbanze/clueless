"""Tests for filler removal.

The negative cases matter most: a filter that eats real words is worse than one
that leaves a few "um"s, because it silently changes what someone said.

Run: python3 -m server.test_textclean
"""

from __future__ import annotations

import sys

from .textclean import strip_fillers

CASES = [
    # Basic hesitations, including punctuation handling.
    ("Um, we are blocked on Kafka.", "we are blocked on Kafka."),
    ("We uh are blocked on Kafka.", "We are blocked on Kafka."),
    ("Uh, um, so the release is blocked.", "so the release is blocked."),
    ("I think, um, that it is done.", "I think, that it is done."),
    ("Hmm. Let me check.", "Let me check."),
    ("Er, the ticket is in review.", "the ticket is in review."),
    # Mid-sentence filler keeps the sentence intact.
    ("The build uh failed again.", "The build failed again."),
    # Stutters.
    ("The the release is blocked.", "The release is blocked."),
    ("I I I think so.", "I think so."),
    # Multi-word hesitations.
    ("You know, the deployment is late.", "the deployment is late."),
    ("It is, I mean, basically done.", "It is, basically done."),
    # Negative cases: real words that must survive.
    ("It works like a cache.", "It works like a cache."),
    ("So we should ship it.", "So we should ship it."),
    ("That is right.", "That is right."),
    ("Well, that depends.", "Well, that depends."),
    ("Ah, I see the issue.", "I see the issue."),
    ("The release is blocked on Kafka.", "The release is blocked on Kafka."),
    # "um" inside a word must not be stripped.
    ("The album is out.", "The album is out."),
    ("Number three is blocked.", "Number three is blocked."),
    # Empty / whitespace input.
    ("", ""),
    ("   ", ""),
]


def main() -> int:
    failures = 0
    for source, expected in CASES:
        actual = strip_fillers(source)
        ok = actual == expected
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {source!r}")
        if not ok:
            print(f"         expected {expected!r}")
            print(f"         actual   {actual!r}")

    print(f"\n===== {'PASS' if failures == 0 else f'{failures} FAILURE(S)'} =====")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
