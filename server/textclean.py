"""Cleanup applied to every transcript turn before it is stored.

Two passes, both deterministic:

  * filler removal — "uh", "um", "erm" and the like are noise in a transcript
    and, worse, they get fed to the language model as if they were content.
  * stutter collapse — the transcriber emits "the the", "I I", "we we" for
    disfluent speech.

Deliberately conservative. Words that *can* be fillers but also carry meaning
("like", "so", "right", "well", "actually") are left alone: dropping "like" from
"it works like a cache" changes the meaning, and a transcript that quietly
deletes real words is worse than one with a few "um"s in it.
"""

from __future__ import annotations

import re

# Pure hesitation sounds. None of these are words in their own right.
_FILLERS = {
    "uh", "uhh", "uhhh", "um", "umm", "ummm", "er", "err", "erm",
    "ah", "ahh", "eh", "hm", "hmm", "hmmm", "mm", "mmm", "mhm",
    "uh-huh", "uhhuh", "mm-hmm", "mmhmm",
}

# Multi-word hesitations. Safe because the phrases are not used to mean
# anything else in a work meeting.
_FILLER_PHRASES = (
    "you know what i mean",
    "you know",
    "i mean",
    "how do you say",
    "what's the word",
    "whats the word",
)

# A word repeated immediately: "the the", "I I", "we we we".
_REPEATED_WORD = re.compile(r"\b(\w+)(\s+\1\b)+", re.IGNORECASE)

# Whitespace before punctuation left behind by removals: "blocked , on".
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")

# Collapse runs of spaces without touching newlines.
_MULTISPACE = re.compile(r"[ \t]{2,}")


def strip_fillers(text: str) -> str:
    """Removes hesitation sounds and immediate word repetitions."""
    if not text:
        return text

    original = text

    for phrase in _FILLER_PHRASES:
        text = re.sub(rf"\b{re.escape(phrase)}\b[,]?\s*", "", text, flags=re.IGNORECASE)

    # Token-wise removal, preserving punctuation attached to a filler
    # ("um," -> ",").
    words = text.split()
    kept: list[str] = []
    for word in words:
        core = word.strip(",.!?;:").lower()
        if core in _FILLERS:
            # Keep trailing punctuation so the sentence still reads correctly —
            # but never duplicate it. "I think, um, that" must not become
            # "I think,, that".
            trailing = word[len(word.rstrip(",.!?;:")):]
            if trailing and kept and not kept[-1].endswith(trailing):
                kept[-1] = kept[-1] + trailing
            continue
        kept.append(word)
    text = " ".join(kept)

    text = _REPEATED_WORD.sub(r"\1", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _MULTISPACE.sub(" ", text).strip()

    # Never return an empty string just because the turn was all fillers —
    # that would silently drop a real (if vacuous) utterance from the record.
    if not text and original.strip():
        return ""
    return text
