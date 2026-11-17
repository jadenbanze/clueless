"""Profile defaults: each persona keeps the guarantees it advertises.

Top-level personas are Developer, Student (which encapsulates Clinical/Medical
rotation mode), and General. Legacy ``PROFILE="medical"`` continues to resolve
to the clinical guarantees.

Run: python3 -m server.test_profiles
"""

from __future__ import annotations

import sys

from . import profiles

failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}")
    if not condition:
        failures.append(name)


def main() -> int:
    print("resolve")
    check("explicit profile wins", profiles.resolve("student", True) == "student")
    check("legacy medical alias resolves", profiles.resolve("medical", True) == "medical")
    check("legacy DEVELOPER_MODE=1 keeps developer", profiles.resolve(None, True) == "developer")
    check("unknown name falls back to general", profiles.resolve("nonsense") == "general")

    print("student + clinical guarantees")
    student = profiles.get("student", clinical=False)
    check("student allows network study sources", student["network_indexing"] is True)
    check("student shows transcript", student["ui"]["show_transcript"] is True)
    check("student offers anki/pubmed/notes", {"notes", "anki", "pubmed"}.issubset(set(student["sources"])))

    for label, clinical in (("student clinical", profiles.get("student", clinical=True)),
                            ("legacy medical", profiles.get("medical"))):
        check(f"{label}: no network indexing", clinical["network_indexing"] is False)
        check(f"{label}: redaction on", clinical["redact_phi"] is True)
        check(f"{label}: not developer mode", clinical["developer_mode"] is False)
        check(f"{label}: transcript hidden", clinical["ui"]["show_transcript"] is False)
        check(f"{label}: larger type", clinical["ui"]["font_scale"] > 1)
        check(f"{label}: only offline sources offered",
              all(not profiles.SOURCE_CATALOG[s]["network"] for s in clinical["sources"]))

    clinical_prompt = profiles.summary_prompt("student", clinical=True)
    check("clinical summary forbids invented dosages", "dosage" in clinical_prompt)
    check("clinical summary avoids identifiers", "the patient" in clinical_prompt)

    print("prompts")
    base = "Rules:\n- one\n"
    check("student addendum appended", profiles.apply_addendum(base, "student") != base)
    check("clinical addendum is a single rule line",
          profiles.apply_addendum(base, "student", clinical=True).count("\n- ") == 2)
    check("developer keeps the four-section summary", "## Action items" in profiles.summary_prompt("developer"))
    check("student summary has key takeaways", "## Key takeaways" in profiles.summary_prompt("student"))
    check("general summary is high level", profiles.summary_prompt("general") != profiles.summary_prompt("developer"))

    print("public view")
    view = profiles.public("student", clinical=True)
    check("no prompt text leaks to UI", "prompt_addendum" not in view and "summary" not in view)
    check("clinical mode surfaced under student", view["name"] == "student" and view["clinical_mode"] is True)
    check("lists three primary profiles (developer, student, general)",
          {p["name"] for p in view["available"]} == {"developer", "student", "general"})
    student_entry = next(p for p in view["available"] if p["name"] == "student")
    check("student advertises clinical sub-mode support", student_entry["supports_clinical"] is True)

    print(f"\n{'PASS' if not failures else f'FAIL ({len(failures)})'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
