"""Suggest CLUELESS_CORRECTIONS pairs from the local index.

Offline: reads Jira/Confluence/code metadata already on disk. Does not call
the network or download models. Paste the printed line into Settings →
Custom vocabulary (or .env) and restart.

Run: python3 -m server.harvest_corrections
"""

from __future__ import annotations

import json
import re
import sys

from . import config

# Tokens too generic to invent corrections for.
_SKIP = {
    "the", "and", "for", "with", "from", "that", "this", "status", "update",
    "issue", "ticket", "page", "file", "test", "tests", "main", "src", "api",
    "app", "service", "services", "data", "user", "users", "true", "false",
    "null", "none", "todo", "fixme", "http", "https", "com", "org",
    "not", "end", "icon", "link", "node", "names", "to", "env", "ui",
}

# Phrase-level STT mangling hints only. Never map a lone common word like
# "coffee" → Kafka — that rewrites ordinary speech (covered by defaults + tests).
_KNOWN_MISHEARS: dict[str, list[str]] = {
    "Kafka": ["cough a", "the cough"],
    "OAuth": ["oh auth"],
    "GraphQL": ["graphic ql"],
    "PostgreSQL": ["post gre SQL"],
    "Postgres": ["postgress"],
    "Terraform": ["terra form"],
    "Kubernetes": ["cube urn etes", "kubernetis"],
    "kubectl": ["kube ctl", "kube control"],
    "JWT": ["jay double you tee"],
    "Confluence": ["con fluence"],
    "Bitbucket": ["bit bucket"],
}

# Cap auto-suggested CamelCase / hyphenated identifiers. Long Java test class
# names are useless as speech corrections and drown out real vocabulary.
_MAX_AUTO_TERM_LEN = 32
_MAX_PAIRS = 40


def _terms_from_metadata(meta: dict) -> set[str]:
    out: set[str] = set()
    for key in ("key", "summary", "title", "repo", "path", "space"):
        value = meta.get(key)
        if not value:
            continue
        text = str(value)
        # Ticket keys, CamelCase tokens, hyphenated product words.
        out.update(re.findall(r"\b[A-Z]{2,}[A-Z0-9]*-\d+\b", text))
        out.update(re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b", text))
        out.update(re.findall(r"\b[A-Z]{3,}\b", text))
        out.update(re.findall(r"\b[A-Za-z][A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b", text))
    return out


def _collect_index_terms() -> set[str]:
    terms: set[str] = set()
    for repo in config.GITHUB_REPOS:
        terms.add(repo.split("/")[-1])
        terms.update(part for part in re.split(r"[-_]", repo.split("/")[-1]) if len(part) > 2)
    if config.JIRA_PROJECT_KEY:
        terms.add(config.JIRA_PROJECT_KEY)

    db = config.CHROMA_DIR / "vectors.sqlite3"
    if not db.exists():
        return terms

    # Raw sqlite only — do not import VectorStore (pulls numpy via the embed
    # path and has segfaulted in constrained environments).
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT metadata FROM items LIMIT 4000").fetchall()
    for (raw,) in rows:
        try:
            meta = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        terms |= _terms_from_metadata(meta)
    return terms


def _suggestions(terms: set[str]) -> list[tuple[str, str]]:
    """Build heard=correct pairs. Longest correct forms first for display."""
    existing = {k.lower() for k in config.SPEECH_CORRECTIONS}
    pairs: list[tuple[str, str]] = []
    seen_wrong: set[str] = set()

    def add(wrong: str, right: str) -> None:
        w = wrong.strip().lower()
        r = right.strip()
        if not w or not r or w == r.lower():
            return
        if w in existing or w in seen_wrong:
            return
        if w in _SKIP:
            return
        seen_wrong.add(w)
        pairs.append((w, r))

    for right, wrongs in _KNOWN_MISHEARS.items():
        # Only suggest known mishears when the canonical term appears in the index
        # or is a universal default we already care about.
        if right in terms or right in config.SPEECH_CORRECTIONS.values():
            for wrong in wrongs:
                add(wrong, right)

    for term in sorted(terms, key=len):
        if len(term) < 4 or len(term) > _MAX_AUTO_TERM_LEN:
            continue
        if term.lower() in _SKIP:
            continue
        # Prefer ticket keys and short product tokens over huge class names.
        if re.fullmatch(r"[A-Z]{2,}[A-Z0-9]*-\d+", term):
            # "PROJ-1234" is spoken as the key; spaced project prefix helps.
            project = term.split("-", 1)[0]
            if len(project) >= 4:
                add(re.sub(r"(?<=.)", " ", project).strip().lower(), project)
            continue
        # Spaced CamelCase only for short identifiers: "BitBucket" → "bit bucket"
        if term[0].isupper() and re.search(r"[a-z][A-Z]", term):
            spaced = re.sub(r"(?<!^)([A-Z])", r" \1", term).strip()
            if " " in spaced and spaced.count(" ") <= 3:
                add(spaced.lower(), term)
        if "-" in term and term.count("-") <= 3:
            add(term.replace("-", " ").lower(), term)

    return pairs[:_MAX_PAIRS]


def main() -> int:
    terms = _collect_index_terms()
    pairs = _suggestions(terms)

    print(f"indexed distinctive terms seen: {len(terms)}")
    print(f"suggested new corrections:     {len(pairs)}")
    print()
    if not pairs:
        print("Nothing new to suggest. Index Jira/code first, or add pairs manually:")
        print('  CLUELESS_CORRECTIONS="heard=Correct,other=Other"')
        return 0

    line = ",".join(f"{w}={r}" for w, r in pairs)
    print("Paste into Settings → Custom vocabulary (then restart), or .env:")
    print()
    print(f"CLUELESS_CORRECTIONS={line}")
    print()
    print("Preview:")
    for wrong, right in pairs:
        print(f"  {wrong!r:40} → {right}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
