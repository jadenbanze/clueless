"""Persona profiles: one switch that tailors prompts, sources, privacy and UI.

A profile is a bundle of defaults, not a separate mode of operation. Every value
it sets can still be overridden individually in Settings; the profile only
decides what you get when you have not.

Kept free of imports from config.py so config can import it to resolve the
active profile without a cycle.
"""

from __future__ import annotations

PROFILES: dict[str, dict] = {
    "developer": {
        "label": "Developer",
        "tagline": "Jira, Confluence and code context, raw logs, every model knob.",
        # Work sources are on: a developer's meetings are about tickets and code.
        "developer_mode": True,
        "network_indexing": True,
        "redact_phi": False,
        "prompt_addendum": (
            "Prefer exact identifiers, commands, file paths and ticket keys when the "
            "sources contain them; never invent one."
        ),
        "summary": "standard",
        "ui": {
            "font_scale": 1.0,
            "show_transcript": True,
            "show_logs": True,
            "advanced_settings": True,
            "minimal": False,
        },
    },
    "medical": {
        "label": "Medical",
        "tagline": "Identifiers redacted on screen, zero network access, larger type.",
        # Network indexers are disabled outright, not just defaulted off: a
        # clinician should not be one stray click from sending anything anywhere.
        "developer_mode": False,
        "network_indexing": False,
        "redact_phi": True,
        "prompt_addendum": (
            "Use plain clinical language. Never guess a dosage, diagnosis, lab value "
            "or patient detail that was not said; if it was not said, say so."
        ),
        "summary": "clinical",
        "ui": {
            "font_scale": 1.2,
            "show_transcript": False,
            "show_logs": False,
            "advanced_settings": False,
            "minimal": True,
            "high_contrast": True,
        },
    },
    "general": {
        "label": "General",
        "tagline": "Simple controls, plain-language answers and high-level recaps.",
        "developer_mode": False,
        "network_indexing": True,
        "redact_phi": False,
        "prompt_addendum": "Answer in one or two plain sentences without jargon.",
        "summary": "high_level",
        "ui": {
            "font_scale": 1.0,
            "show_transcript": True,
            "show_logs": False,
            "advanced_settings": False,
            "minimal": False,
            "simple_toggles": True,
        },
    },
}

DEFAULT_PROFILE = "general"

SUMMARY_PROMPTS = {
    # The four-section structure every summary test asserts against.
    "standard": """You summarise a work meeting from its transcript.

Produce markdown with exactly these sections:

## Summary
2-4 sentences on what the meeting was about.

## Decisions
Bullets. Only decisions actually made. Write "None recorded" if there are none.

## Action items
Bullets formatted "owner — action". The owner must be a person's name that was
actually said aloud. If no name was given, write "Unassigned — action".
Never use the transcript speaker labels "me" or "them" as an owner; they are
transcription artefacts, not names.

## Open questions
Bullets. Unresolved issues or things someone needs to follow up.

Rules:
- Only use what is in the transcript. Do not infer facts that were not said.
- If a section has nothing, write "None recorded".
- Attribute statements to speakers using the transcript labels (me/them).
""",
    "high_level": """You recap a meeting for someone who was not there.

Produce markdown with exactly these sections:

## What happened
2-3 plain sentences. No jargon.

## Decisions
Bullets. Only decisions actually made. Write "None recorded" if there are none.

## Next steps
Bullets formatted "owner — action". The owner must be a name said aloud, or
"Unassigned". Never use the labels "me" or "them" as an owner.

Rules:
- Only use what is in the transcript. Do not infer facts that were not said.
- If a section has nothing, write "None recorded".
""",
    "clinical": """You summarise a clinical or care-team discussion from its transcript.

Produce markdown with exactly these sections:

## Summary
2-4 sentences on what was discussed.

## Decisions
Bullets. Only decisions actually made. Write "None recorded" if there are none.

## Action items
Bullets formatted "owner — action". The owner must be a name said aloud, or
"Unassigned". Never use the labels "me" or "them" as an owner.

## Open questions
Bullets. Unresolved issues, pending results, or things to follow up.

Rules:
- Only use what is in the transcript. Never add a dosage, diagnosis, value or
  instruction that was not said.
- Do not repeat patient identifiers (names, dates of birth, record numbers,
  contact details); refer to "the patient".
- If a section has nothing, write "None recorded".
""",
}


def resolve(name: str | None, developer_mode_flag: bool = False) -> str:
    """Picks the active profile.

    Installs that predate profiles set DEVELOPER_MODE=1 instead; keep them on
    the developer profile rather than silently changing their behaviour.
    """
    if name in PROFILES:
        return name
    return "developer" if developer_mode_flag else DEFAULT_PROFILE


def get(name: str) -> dict:
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE])


def apply_addendum(system_prompt: str, name: str) -> str:
    addendum = get(name).get("prompt_addendum")
    return f"{system_prompt.rstrip()}\n- {addendum}\n" if addendum else system_prompt


def summary_prompt(name: str) -> str:
    return SUMMARY_PROMPTS[get(name).get("summary", "standard")]


def public(name: str) -> dict:
    """What the UI needs: everything except prompt text."""
    profile = get(name)
    return {
        "name": name if name in PROFILES else DEFAULT_PROFILE,
        "label": profile["label"],
        "tagline": profile["tagline"],
        "ui": profile["ui"],
        "network_indexing": profile["network_indexing"],
        "redact_phi": profile["redact_phi"],
        "available": [
            {"name": key, "label": value["label"], "tagline": value["tagline"]}
            for key, value in PROFILES.items()
        ],
    }
