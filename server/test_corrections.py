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
    # Generic engineering vocabulary.
    ("o auth token refresh", "OAuth token refresh"),
    ("graph ql schema", "GraphQL schema"),
    ("postgress migration failed", "Postgres migration failed"),
    ("terra form plan", "Terraform plan"),
    ("kube ctl apply", "kubectl apply"),
    ("git hub pull request", "GitHub pull request"),
    ("ci cd pipeline", "CI/CD pipeline"),
    ("json web token expiry", "JWT expiry"),
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
    vocabulary without editing code. Values come from settings.json via
    _setting(), with env as fallback — the Settings window writes the file.
    """
    import importlib
    import json
    import os
    import tempfile
    from pathlib import Path

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / ".cache").mkdir()
        (data_dir / ".cache" / "settings.json").write_text(json.dumps({
            # A name the transcriber spells inconsistently is the common case.
            "CLUELESS_CORRECTIONS": "acme=ACME,metformin=Metformin,hipaa=HIPAA,preeya=Priya,pria=Priya",
        }))
        os.environ["CLUELESS_DATA_DIR"] = str(data_dir)
        os.environ.pop("CLUELESS_CORRECTIONS", None)

        from server import config as cfg

        importlib.reload(cfg)
        importlib.reload(importlib.import_module("server.socket_server"))
        from server.socket_server import apply_corrections

        for source, expected in [
            ("the acme project", "the ACME project"),
            ("metformin dose", "Metformin dose"),
            ("hipaa compliance", "HIPAA compliance"),
            ("Preeya is investigating.", "Priya is investigating."),
            ("PRIA is investigating.", "Priya is investigating."),
            ("unrelated text", "unrelated text"),
        ]:
            actual = apply_corrections(source)
            ok = actual == expected
            if not ok:
                failures += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] settings.json: {source!r} -> {actual!r}")

        os.environ.pop("CLUELESS_DATA_DIR", None)
        importlib.reload(cfg)
        importlib.reload(importlib.import_module("server.socket_server"))
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
