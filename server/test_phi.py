"""Identifier redaction: each positive case is caught, each negative left alone.

The negative cases matter as much: a redactor that eats "Docker" or "May I ask"
makes the transcript unreadable, and people switch it off.

Run: python3 -m server.test_phi
"""

from __future__ import annotations

import sys

from .phi import redact

CASES = [
    # Positive
    ("Dr. Patel will review the scan.", "Dr [NAME] will review the scan."),
    ("Ask Nurse Okafor about it", "Nurse [NAME] about it"),
    ("patient Maria Lopez was admitted", "patient [NAME] was admitted"),
    ("DOB 04/12/1980, allergic to penicillin", "[DOB], allergic to penicillin"),
    ("born March 3, 1975", "[DOB]"),
    ("MRN 448812 on the chart", "[MRN] on the chart"),
    ("medical record number A-99812", "[MRN]"),
    ("call 555-201-3344 after rounds", "call [PHONE] after rounds"),
    ("reach her at (555) 201 3344", "reach her at [PHONE]"),
    ("email j.doe@example.org", "email [EMAIL]"),
    ("SSN 123-45-6789", "SSN [SSN]"),
    ("lives at 42 Elm Street now", "lives at [ADDRESS] now"),
    ("follow up on 11/02/2026", "follow up on [DATE]"),
    ("seen on January 14th", "seen on [DATE]"),
    # Negative
    ("Docker build failed on the CI runner", "Docker build failed on the CI runner"),
    ("May I ask a question?", "May I ask a question?"),
    ("the patient is stable", "the patient is stable"),
    ("increase to 20 mg twice daily", "increase to 20 mg twice daily"),
    ("version 1.2.3 shipped", "version 1.2.3 shipped"),
    ("we have 3 beds on 4 West", "we have 3 beds on 4 West"),
    ("Dr. said it was fine", "Dr. said it was fine"),
    ("", ""),
]


def main() -> int:
    failures = 0
    for source, expected_fragment in CASES:
        actual = redact(source)
        ok = expected_fragment in actual if source else actual == ""
        failures += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {source!r} -> {actual!r}")
    print(f"===== {'PASS' if not failures else f'{failures} FAILURE(S)'} =====")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
