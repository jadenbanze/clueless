"""Redaction of identifiers from text shown, copied or exported.

Scope, stated plainly so nobody over-trusts it: this is pattern-based redaction
of the identifier types that appear in spoken clinical conversation — names
following a title, dates, record numbers, phone numbers, emails, street
addresses and SSNs. It is defence in depth for a screen that might be glimpsed
or a summary that might be pasted somewhere, not a de-identification guarantee.
A name spoken without a title ("tell Maria") is not caught.

The raw transcript never leaves the process; it is what the local model reads.
Only what is rendered, copied, logged or exported passes through here.
"""

from __future__ import annotations

import re

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_NAME = r"[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+)?"

# Order matters: the specific labelled forms run before the generic ones, so
# "DOB 04/12/1980" becomes [DOB] rather than "DOB [DATE]".
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(?:MRN|medical record(?: number)?|record number|chart number)\b[\s:#-]*[A-Z0-9-]*\d[A-Z0-9-]*",
                re.IGNORECASE), "[MRN]"),
    (re.compile(rf"\b(?:DOB|date of birth|born on|born)\b[\s:,-]*"
                rf"(?:\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})",
                re.IGNORECASE), "[DOB]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}"
                r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Way|Court|Ct|Place|Pl|Terrace)\b\.?"),
     "[ADDRESS]"),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "[DATE]"),
    (re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b"), "[DATE]"),
    # Title + name. Case-sensitive on the name so "Dr. said" is left alone; the
    # title itself is kept because it carries meaning ("the doctor").
    (re.compile(rf"\b(Dr|Doctor|Mr|Mrs|Ms|Miss|Mx|Nurse|Prof)\.?\s+{_NAME}"), r"\1 [NAME]"),
    (re.compile(rf"\b([Pp]atient|[Rr]esident|[Cc]lient)\s+(?!is\b|was\b|has\b|had\b|and\b|in\b){_NAME}"),
     r"\1 [NAME]"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def maybe_redact(text: str, enabled: bool) -> str:
    return redact(text) if enabled else text
