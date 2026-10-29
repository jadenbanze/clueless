"""Trust check for Foundation Models answers.

Foundation Models is ~4x faster than the 8B Ollama model but unreliable at
multi-rule grounding. Measured failures:

  * asked "what is blocking the release?" against a transcript saying exactly
    that, it answered "No info in the loaded context"
  * leaked an unrelated rule ("I can't identify participants from audio") into a
    ticket question
  * emitted markdown headings despite being told not to

The idea here is to keep the fast model's answer when it is obviously fine, and
pay for the slow model only when the answer looks wrong. The economics depend
entirely on the fallback rate, so this module is deliberately conservative: a
false "trusted" verdict is a wrong answer in a meeting, while a false "distrust"
only costs latency.
"""

from __future__ import annotations

import re

# Words that carry no signal when checking whether a question is answerable from
# a transcript.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "of",
    "to", "in", "on", "for", "with", "at", "by", "from", "about", "what", "who",
    "when", "where", "why", "how", "which", "does", "do", "did", "we", "our",
    "you", "your", "it", "its", "this", "that", "there", "and", "or", "not",
    "any", "can", "could", "should", "would", "will", "have", "has", "had",
}

_ABSTENTION = "no info in the loaded context"
_PARTICIPANT_REFUSAL = "can't identify participants"


def _content_words(text: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]{4,}", text.lower())
        if word not in _STOPWORDS
    }


def answer_is_unreliable(answer: str, question: str, transcript: str) -> tuple[bool, str]:
    """Returns (unreliable, reason).

    Reasons are returned so the caller can log *why* a fallback happened; a
    hybrid that silently falls back half the time would be indistinguishable
    from just using the slow model.
    """
    text = (answer or "").strip()
    if not text:
        return True, "empty answer"

    lowered = text.lower()

    if len(text) < 8:
        return True, "answer too short"

    # Markdown structure despite instructions: a reliable signal that the model
    # stopped following the format rules, which correlates with ignoring others.
    if re.search(r"^\s*#{1,6}\s", text, re.MULTILINE) or lowered.startswith("**"):
        return True, "markdown structure"

    # The participant rule leaking into an unrelated question.
    if _PARTICIPANT_REFUSAL in lowered and not re.search(
        r"\b(who|presenting|attending|speaking)\b", question.lower()
    ):
        return True, "unrelated refusal"

    # Abstention on a question the transcript can actually answer. This is the
    # most important check: it is the failure that silently produces "no info"
    # for something plainly stated.
    if _ABSTENTION in lowered:
        asked = _content_words(question)
        if not asked:
            return False, "abstention with no content words to verify"
        spoken = _content_words(transcript)
        overlap = asked & spoken
        if len(overlap) >= 2:
            return True, f"abstained despite transcript covering {sorted(overlap)[:3]}"
        return False, "abstention with no transcript overlap"

    return False, "looks reliable"
