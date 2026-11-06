"""Profile defaults: each persona keeps the guarantees it advertises.

The medical profile's promises (no outbound indexing, redaction on, clinical
summary) are what make it safe to recommend, so they are asserted rather than
trusted to survive edits to the profile table.

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
    check("explicit profile wins", profiles.resolve("medical", True) == "medical")
    check("legacy DEVELOPER_MODE=1 keeps developer", profiles.resolve(None, True) == "developer")
    check("unknown name falls back to general", profiles.resolve("nonsense") == "general")

    print("medical guarantees")
    medical = profiles.get("medical")
    check("no network indexing", medical["network_indexing"] is False)
    check("redaction on", medical["redact_phi"] is True)
    check("not developer mode", medical["developer_mode"] is False)
    check("transcript hidden", medical["ui"]["show_transcript"] is False)
    check("larger type", medical["ui"]["font_scale"] > 1)
    clinical = profiles.summary_prompt("medical")
    check("clinical summary forbids invented dosages", "dosage" in clinical)
    check("clinical summary avoids identifiers", "the patient" in clinical)

    print("prompts")
    base = "Rules:\n- one\n"
    check("medical addendum appended", profiles.apply_addendum(base, "medical") != base)
    check("addendum is a single rule line", profiles.apply_addendum(base, "medical").count("\n- ") == 2)
    check("developer keeps the four-section summary", "## Action items" in profiles.summary_prompt("developer"))
    check("general summary is high level", profiles.summary_prompt("general") != profiles.summary_prompt("developer"))

    print("public view")
    view = profiles.public("medical")
    check("no prompt text leaks to UI", "prompt_addendum" not in view and "summary" not in view)
    check("lists all three profiles", {p["name"] for p in view["available"]} == {"developer", "medical", "general"})

    print(f"\n{'PASS' if not failures else f'FAIL ({len(failures)})'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
