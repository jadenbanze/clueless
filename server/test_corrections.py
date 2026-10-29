"""Tests for the deterministic transcription-correction layer.

Fast, offline, and covers the negative cases — a correction that fires inside an
unrelated word is worse than no correction at all, because it silently rewrites
what someone actually said.

Run: python3 -m server.test_corrections
"""

from __future__ import annotations

import sys

from . import config
from .socket_server import apply_corrections

# (input, expected)
CASES = [
    # Names, across the variants actually observed in transcripts.
    ("Shwada is investigating the JWT processing error.",
     "Shweta is investigating the JWT processing error."),
    ("Schweda is investigating.", "Shweta is investigating."),
    ("shweda is investigating.", "Shweta is investigating."),
    ("SHWADA is investigating.", "Shweta is investigating."),
    # Read-only, hyphenated and spaced.
    ("We need a decision on red-only access.",
     "We need a decision on read-only access."),
    ("decision on red only access", "decision on read only access"),
    ("redonly access", "read-only access"),
    # Casing normalisation for acronyms and product names.
    ("kafka authentication failure", "Kafka authentication failure"),
    ("the jwt processing error", "the JWT processing error"),
    # Organisation-specific terms are deliberately NOT defaults — see the
    # user-supplied section below. Shipped defaults must be generic.
    # Phrase-level repairs from the microphone path.
    ("coffee authentication failure is blocking", "Kafka authentication failure is blocking"),
    ("the audit right path remediation", "the audit write path remediation"),
    # Negative cases: must not touch unrelated text.
    ("a red herring in the hallway", "a red herring in the hallway"),
    ("the red door", "the red door"),
    ("I need a coffee", "I need a coffee"),
    ("the right path is unclear", "the right path is unclear"),
    ("Kafkaesque bureaucracy", "Kafkaesque bureaucracy"),
    ("Nothing to correct here.", "Nothing to correct here."),
    ("", ""),
]


def check_user_supplied() -> int:
    """CLUELESS_CORRECTIONS must extend the defaults at import time.

    This is how a medical, legal or finance user teaches the transcriber their
    vocabulary without editing code.
    """
    import importlib
    import os

    os.environ["CLUELESS_CORRECTIONS"] = "acme=ACME,metformin=Metformin,hipaa=HIPAA"
    from server import config as cfg

    importlib.reload(cfg)
    failures = 0
    for source, expected in [
        ("the acme project", "the ACME project"),
        ("metformin dose", "Metformin dose"),
        ("hipaa compliance", "HIPAA compliance"),
        ("unrelated text", "unrelated text"),
    ]:
        actual = cfg.SPEECH_CORRECTIONS and None
        from server.socket_server import apply_corrections
        importlib.reload(importlib.import_module("server.socket_server"))
        actual = apply_corrections(source)
        ok = actual == expected
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] user-supplied: {source!r} -> {actual!r}")
    os.environ.pop("CLUELESS_CORRECTIONS", None)
    return failures


def main() -> int:
    failures = 0
    for source, expected in CASES:
        actual = apply_corrections(source)
        ok = actual == expected
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {source[:44]!r}")
        if not ok:
            print(f"         expected {expected!r}")
            print(f"         actual   {actual!r}")

    print("\n  user-supplied vocabulary:")
    failures += check_user_supplied()

    print(f"\n  corrections configured: {len(config.SPEECH_CORRECTIONS)}")
    print(f"===== {'PASS' if failures == 0 else f'{failures} FAILURE(S)'} =====")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
