"""Persona profiles: one switch that tailors prompts, sources, privacy and UI.

Three primary user-facing profiles:
  * developer — software teams (Jira, Confluence, code/PRs, local docs, logs)
  * student   — lectures, research & medical school; encapsulates Clinical
                rotation mode (HIPAA-aware, zero network, identifier redaction)
  * general   — everyday meetings (local notes, Notion, calendar, screen)

Legacy ``PROFILE="medical"`` resolves cleanly to the clinical configuration so
existing settings and clinical safety tests keep every guarantee intact.

Kept free of imports from config.py so config can import it without a cycle.
"""

from __future__ import annotations

# Metadata for every toggleable context source across profiles.
SOURCE_CATALOG: dict[str, dict] = {
    "jira": {"label": "Jira", "short": "Jira", "network": True},
    "confluence": {"label": "Confluence", "short": "Wiki", "network": True},
    "code": {"label": "GitHub & PRs", "short": "Code", "network": True},
    "notes": {"label": "Local files", "short": "Notes", "network": False},
    "anki": {"label": "Anki decks", "short": "Anki", "network": False},
    "pubmed": {"label": "PubMed & Zotero", "short": "Papers", "network": True},
    "canvas": {"label": "Canvas LMS", "short": "Canvas", "network": True},
    "notion": {"label": "Notion", "short": "Notion", "network": True},
    "calendar": {"label": "Calendar", "short": "Cal", "network": False},
    "screen": {"label": "Screen OCR", "short": "Screen", "network": False},
}

_CLINICAL_PROFILE: dict = {
    "label": "Student (Clinical)",
    "tagline": "Identifiers redacted on screen, zero network access, larger type.",
    "developer_mode": False,
    "student_mode": True,
    "clinical_mode": True,
    # Network indexers are disabled outright, not just defaulted off: a
    # clinical session must never be one stray click from outbound traffic.
    "network_indexing": False,
    "redact_phi": True,
    "prompt_addendum": (
        "Use plain clinical language. Never guess a dosage, diagnosis, lab value "
        "or patient detail that was not said; if it was not said, say so."
    ),
    "summary": "clinical",
    "sources": ("notes", "anki", "screen"),
    "default_sources": {"notes": True, "anki": False, "screen": False},
    "hotkey_sources": ("notes", "anki", "screen"),
    "ui": {
        "font_scale": 1.2,
        "show_transcript": False,
        "show_logs": False,
        "advanced_settings": False,
        "minimal": True,
        "high_contrast": True,
        "supports_clinical": True,
    },
}

PROFILES: dict[str, dict] = {
    "developer": {
        "label": "Developer",
        "tagline": "Jira, Confluence, code & PRs, local docs, raw logs and model tuning.",
        "developer_mode": True,
        "student_mode": False,
        "clinical_mode": False,
        "network_indexing": True,
        "redact_phi": False,
        "prompt_addendum": (
            "Prefer exact identifiers, commands, file paths and ticket keys when the "
            "sources contain them; never invent one."
        ),
        "summary": "standard",
        "sources": ("jira", "confluence", "code", "notes", "calendar", "screen"),
        "default_sources": {
            "jira": True,
            "confluence": True,
            "code": False,
            "notes": True,
            "calendar": True,
            "screen": False,
        },
        "hotkey_sources": ("jira", "confluence", "code", "screen"),
        "ui": {
            "font_scale": 1.0,
            "show_transcript": True,
            "show_logs": True,
            "advanced_settings": True,
            "minimal": False,
            "supports_clinical": False,
        },
    },
    "student": {
        "label": "Student",
        "tagline": "Lectures, research & med school — slides/PDFs, Anki, PubMed, Canvas, plus Clinical mode.",
        "developer_mode": False,
        "student_mode": True,
        "clinical_mode": False,
        "network_indexing": True,
        "redact_phi": False,
        "prompt_addendum": (
            "Explain concepts clearly using exact terminology, mechanisms or findings "
            "from the lecture and reference sources; never invent citations, dosages or facts."
        ),
        "summary": "academic",
        "sources": ("notes", "anki", "pubmed", "canvas", "notion", "calendar", "screen"),
        "default_sources": {
            "notes": True,
            "anki": True,
            "pubmed": True,
            "canvas": False,
            "notion": False,
            "calendar": True,
            "screen": False,
        },
        "hotkey_sources": ("notes", "anki", "pubmed", "screen"),
        "ui": {
            "font_scale": 1.0,
            "show_transcript": True,
            "show_logs": False,
            "advanced_settings": False,
            "minimal": False,
            "supports_clinical": True,
        },
    },
    "general": {
        "label": "General",
        "tagline": "Simple controls, plain-language answers, local notes, Notion and calendar.",
        "developer_mode": False,
        "student_mode": False,
        "clinical_mode": False,
        "network_indexing": True,
        "redact_phi": False,
        "prompt_addendum": "Answer in one or two plain sentences without jargon.",
        "summary": "high_level",
        "sources": ("notes", "notion", "calendar", "screen"),
        "default_sources": {
            "notes": True,
            "notion": False,
            "calendar": True,
            "screen": False,
        },
        "hotkey_sources": ("notes", "notion", "calendar", "screen"),
        "ui": {
            "font_scale": 1.0,
            "show_transcript": True,
            "show_logs": False,
            "advanced_settings": False,
            "minimal": False,
            "simple_toggles": True,
            "supports_clinical": False,
        },
    },
    # Legacy alias retained so existing configs and clinical safety tests resolve directly.
    "medical": {**_CLINICAL_PROFILE, "label": "Medical"},
}

PRIMARY_PROFILES = ("developer", "student", "general")
DEFAULT_PROFILE = "general"

SUMMARY_PROMPTS = {
    # The four-section structure every developer summary test asserts against.
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
    "academic": """You summarise a lecture, seminar, study group or case discussion from its transcript.

Produce markdown with exactly these sections:

## Overview
2-4 sentences on the main topic and scope covered.

## Key takeaways
Bullets. Core concepts, mechanisms, definitions or high-yield points stated aloud. Write "None recorded" if there are none.

## Action items
Bullets formatted "owner — action" for assignments, readings or follow-ups (or "Unassigned — action" if no name was said). Never use "me" or "them" as an owner.

## Questions to review
Bullets. Unresolved questions, unclear points, or topics flagged for further study.

Rules:
- Only use what is in the transcript. Never invent facts, citations or dosages that were not said.
- If a section has nothing, write "None recorded".
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


def get(name: str, *, clinical: bool = False) -> dict:
    if name == "medical" or (name == "student" and clinical):
        return PROFILES["medical"] if name == "medical" else _CLINICAL_PROFILE
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE])


def apply_addendum(system_prompt: str, name: str, *, clinical: bool = False) -> str:
    addendum = get(name, clinical=clinical).get("prompt_addendum")
    return f"{system_prompt.rstrip()}\n- {addendum}\n" if addendum else system_prompt


def summary_prompt(name: str, *, clinical: bool = False) -> str:
    return SUMMARY_PROMPTS[get(name, clinical=clinical).get("summary", "standard")]


def public(name: str, *, clinical: bool = False) -> dict:
    """What the UI needs: everything except prompt text."""
    effective = get(name, clinical=clinical)
    # Surface legacy "medical" as "student" + clinical_mode=True in the 3-card picker
    # while reporting effective clinical guarantees accurately.
    selected = "student" if name == "medical" else (name if name in PRIMARY_PROFILES else DEFAULT_PROFILE)
    is_clinical = bool(effective.get("clinical_mode"))
    return {
        "name": selected,
        "effective_name": "medical" if is_clinical else selected,
        "clinical_mode": is_clinical,
        "label": effective["label"],
        "tagline": effective["tagline"],
        "ui": effective["ui"],
        "network_indexing": effective["network_indexing"],
        "redact_phi": effective["redact_phi"],
        "sources": list(effective.get("sources", ())),
        "hotkey_sources": list(effective.get("hotkey_sources", ())),
        "source_labels": {k: v["short"] for k, v in SOURCE_CATALOG.items()},
        "available": [
            {
                "name": key,
                "label": PROFILES[key]["label"],
                "tagline": PROFILES[key]["tagline"],
                "supports_clinical": bool(PROFILES[key]["ui"].get("supports_clinical")),
            }
            for key in PRIMARY_PROFILES
        ],
    }
