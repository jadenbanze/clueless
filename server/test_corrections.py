"""Tests for the transcription-correction, phonetic repair, and echo-bleed layers.

Run: python3 -m server.test_corrections
"""

from __future__ import annotations

import sys

from . import config
from .socket_server import apply_corrections, is_acoustic_echo, rebuild_corrections

CASES = [
    ("We need a decision on red-only access.", "We need a decision on read-only access."),
    ("decision on red only access", "decision on read only access"),
    ("redonly access", "read-only access"),
    ("kafka authentication failure", "Kafka authentication failure"),
    ("the jwt processing error", "the JWT processing error"),
    ("coffee authentication failure is blocking", "Kafka authentication failure is blocking"),
    ("the audit right path remediation", "the audit write path remediation"),
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
    import json
    import os
    import tempfile
    from pathlib import Path

    failures = 0
    saved_env = os.environ.get("CLUELESS_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / ".cache").mkdir()
        (data_dir / ".cache" / "settings.json").write_text(json.dumps({
            "CLUELESS_CORRECTIONS": "acme=ACME,metformin=Metformin,hipaa=HIPAA,preeya=Priya,pria=Priya",
        }))
        (data_dir / ".cache" / "auto-vocabulary.json").write_text(json.dumps({
            "pairs": {"spiro lactone": "Spironolactone"},
            "terms": ["Priya", "Spironolactone", "Cardiomyopathy"],
        }))
        os.environ["CLUELESS_DATA_DIR"] = str(data_dir)
        os.environ.pop("CLUELESS_CORRECTIONS", None)

        config.reload()
        rebuild_corrections()

        for source, expected in [
            ("the acme project", "the ACME project"),
            ("metformin dose", "Metformin dose"),
            ("hipaa compliance", "HIPAA compliance"),
            ("Preeya is investigating.", "Priya is investigating."),
            ("PRIA is investigating.", "Priya is investigating."),
            # Phonetic proper-noun repair on unmapped near-miss spelling:
            ("Priyah reviewed the chart.", "Priya reviewed the chart."),
            ("spiro lactone mechanism", "Spironolactone mechanism"),
            # Negative: must never rewrite common capitalized words or longer derivatives
            ("Docker build passed on Monday.", "Docker build passed on Monday."),
            ("Kafkaesque bureaucracy", "Kafkaesque bureaucracy"),
            ("unrelated text", "unrelated text"),
        ]:
            actual = apply_corrections(source)
            ok = actual == expected
            if not ok:
                failures += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] settings/vocab: {source!r} -> {actual!r}")

        if saved_env is None:
            os.environ.pop("CLUELESS_DATA_DIR", None)
        else:
            os.environ["CLUELESS_DATA_DIR"] = saved_env
        config.reload()
        rebuild_corrections()
    return failures


def check_echo_bleed() -> int:
    failures = 0
    cases = [
        # Laptop speaker bleed picked up 400ms later on mic -> drop
        ("Are we blocked on Kafka authentication", 100.4, "Are we still blocked on Kafka authentication?", 100.0, True),
        # Genuine reply -> keep
        ("Yes, Priya is fixing the token validator now.", 101.2, "Are we still blocked on Kafka authentication?", 100.0, False),
        # Same words much later (>2.5s) -> keep (user repeating intentionally)
        ("Are we blocked on Kafka authentication", 105.0, "Are we still blocked on Kafka authentication?", 100.0, False),
        # Short acknowledgement ("yes", "right") -> never drop
        ("Yes right", 100.3, "Yes right let us check", 100.0, False),
    ]
    for me_t, me_ts, them_t, them_ts, expected in cases:
        got = is_acoustic_echo(me_t, me_ts, them_t, them_ts)
        ok = got is expected
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] echo={got} (expected {expected}): {me_t[:36]!r}")
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

    print("\n  user-supplied & phonetic vocabulary:")
    failures += check_user_supplied()

    print("\n  mic acoustic-bleed suppression:")
    failures += check_echo_bleed()

    print(f"\n  corrections configured: {len(config.SPEECH_CORRECTIONS)}")
    print(f"===== {'PASS' if failures == 0 else f'{failures} FAILURE(S)'} =====")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
