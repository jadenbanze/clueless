"""Harvest domain vocabulary from the local index into auto-vocabulary.json.

Offline: reads metadata already stored in SQLite (tickets, pages, code, Anki
decks, papers, courses) plus optional attendee names. Never calls the network.

Run: python3 -m server.harvest_corrections
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys

from . import config, socket_server

_SKIP = {
    "the", "and", "for", "with", "from", "that", "this", "status", "update",
    "issue", "ticket", "page", "file", "test", "tests", "main", "src", "api",
    "app", "service", "services", "data", "user", "users", "true", "false",
    "null", "none", "todo", "fixme", "http", "https", "com", "org",
    "not", "end", "icon", "link", "node", "names", "to", "env", "ui",
}

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
    "Anki": ["anky", "aunty"],
    "PubMed": ["pub med"],
    "Zotero": ["zo tero"],
}

_MAX_AUTO_TERM_LEN = 32
_MAX_PAIRS = 60


def _terms_from_metadata(meta: dict) -> set[str]:
    out: set[str] = set()
    for key in ("key", "summary", "title", "repo", "path", "space", "assignee", "deck", "course", "front"):
        value = meta.get(key)
        if not value:
            continue
        text = str(value)
        out.update(re.findall(r"\b[A-Z]{2,}[A-Z0-9]*-\d+\b", text))
        out.update(re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b", text))
        out.update(re.findall(r"\b[A-Z]{3,}\b", text))
        out.update(re.findall(r"\b[A-Za-z][A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b", text))
        # Multi-character capitalized proper names (e.g. assignees, drug/anatomical terms).
        if key in ("assignee", "deck", "front", "title"):
            out.update(re.findall(r"\b[A-Z][a-z]{4,24}\b", text))
    return out


def _collect_index_terms(extra_names: list[str] | None = None) -> set[str]:
    terms: set[str] = set()
    for repo in config.GITHUB_REPOS:
        terms.add(repo.split("/")[-1])
        terms.update(part for part in re.split(r"[-_]", repo.split("/")[-1]) if len(part) > 2)
    if config.JIRA_PROJECT_KEY:
        terms.add(config.JIRA_PROJECT_KEY)
    if config.USER_NAME:
        terms.update(part for part in config.USER_NAME.split() if len(part) >= 4)
    for name in extra_names or ():
        terms.update(part for part in str(name).split() if len(part) >= 4 and part[0].isupper())

    db = config.CHROMA_DIR / "vectors.sqlite3"
    if not db.exists():
        return terms

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT metadata FROM items LIMIT 5000").fetchall()
    for (raw,) in rows:
        try:
            meta = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        terms |= _terms_from_metadata(meta)
    return terms


def _suggestions(terms: set[str]) -> list[tuple[str, str]]:
    existing = {k.lower() for k in config.BASE_SPEECH_CORRECTIONS}
    pairs: list[tuple[str, str]] = []
    seen_wrong: set[str] = set()

    def add(wrong: str, right: str) -> None:
        w = wrong.strip().lower()
        r = right.strip()
        if not w or not r or w == r.lower():
            return
        if w in existing or w in seen_wrong or w in _SKIP:
            return
        seen_wrong.add(w)
        pairs.append((w, r))

    for right, wrongs in _KNOWN_MISHEARS.items():
        if right in terms or right in config.SPEECH_CORRECTIONS.values():
            for wrong in wrongs:
                add(wrong, right)

    for term in sorted(terms, key=len):
        if len(term) < 4 or len(term) > _MAX_AUTO_TERM_LEN:
            continue
        if term.lower() in _SKIP:
            continue
        if re.fullmatch(r"[A-Z]{2,}[A-Z0-9]*-\d+", term):
            project = term.split("-", 1)[0]
            if len(project) >= 4:
                add(" ".join(project).lower(), project)
            continue
        if term[0].isupper() and re.search(r"[a-z][A-Z]", term):
            spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", term).strip()
            if " " in spaced and spaced.count(" ") <= 3:
                add(spaced.lower(), term)
        if "-" in term and term.count("-") <= 3:
            add(term.replace("-", " ").lower(), term)

    return pairs[:_MAX_PAIRS]


def harvest_to_cache(extra_names: list[str] | None = None) -> dict:
    """Writes auto-harvested corrections and proper nouns to .cache/auto-vocabulary.json."""
    terms = _collect_index_terms(extra_names=extra_names)
    pairs = _suggestions(terms)
    proper = sorted(
        t for t in terms
        if t.isalpha() and t[0].isupper() and 5 <= len(t) <= 28 and t.lower() not in _SKIP
    )[:300]

    payload = {
        "pairs": {w: r for w, r in pairs},
        "terms": proper,
    }
    config.AUTO_VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.AUTO_VOCAB_PATH.write_text(json.dumps(payload, indent=1))
    config.reload()
    socket_server.rebuild_corrections(proper)
    return {"terms_count": len(terms), "pairs_count": len(pairs), "pairs": payload["pairs"]}


def main() -> int:
    terms = _collect_index_terms()
    pairs = _suggestions(terms)

    print(f"indexed distinctive terms seen: {len(terms)}")
    print(f"suggested new corrections:     {len(pairs)}")
    print()
    if not pairs:
        print("Nothing new to suggest. Index sources first, or add pairs manually:")
        print('  CLUELESS_CORRECTIONS="heard=Correct,other=Other"')
        return 0

    line = ",".join(f"{w}={r}" for w, r in pairs)
    print("Paste into Settings → Speech → Custom vocabulary, or .env:")
    print()
    print(f"CLUELESS_CORRECTIONS={line}")
    print()
    print("Preview:")
    for wrong, right in pairs:
        print(f"  {wrong!r:40} -> {right}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
