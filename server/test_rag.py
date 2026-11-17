"""Offline unit tests for hybrid VectorStore + FTS5 BM25 + RRF retrieval.

Run: python3 -m server.test_rag
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from . import config, providers, rag
from .providers.base import ProviderError
from .vectorstore import VectorStore

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def main() -> int:
    embed_calls = 0

    def fake_embed(texts: list[str]) -> list[list[float]]:
        nonlocal embed_calls
        embed_calls += 1
        out = []
        for t in texts:
            low = t.lower()
            # Simple deterministic 4-D toy embedding
            out.append([
                1.0 if "cardiology" in low or "heart" in low else 0.1,
                1.0 if "kafka" in low or "broker" in low else 0.1,
                1.0 if "write" in low else (0.95 if "read" in low else 0.1),
                0.2,
            ])
        return out

    saved_embed = providers.embed
    saved_chroma = config.CHROMA_DIR

    with tempfile.TemporaryDirectory() as tmp:
        config.CHROMA_DIR = Path(tmp)
        rag._embed_cache.clear()
        providers.embed = fake_embed

        try:
            store = rag.Store()
            store.add(
                config.JIRA_COLLECTION,
                ["PROJ-101", "PROJ-202"],
                [
                    "PROJ-101: Audit read path investigation\nStatus: Open",
                    "PROJ-202: Audit write path remediation\nStatus: In Progress",
                ],
                [
                    {"source": "jira", "key": "PROJ-101", "summary": "Audit read path", "url": "https://example.atlassian.net/browse/PROJ-101"},
                    {"source": "jira", "key": "PROJ-202", "summary": "Audit write path", "url": "https://example.atlassian.net/browse/PROJ-202"},
                ],
            )
            store.add(
                config.NOTES_COLLECTION,
                ["note:1", "note:2"],
                [
                    "Cardiology lecture: beta blockers reduce myocardial oxygen demand.",
                    "Nephrology lecture: loop diuretics inhibit NKCC2 in the thick ascending limb.",
                ],
                [
                    {"source": "notes", "title": "cardiology.pdf", "location": "p. 4", "local_path": "/tmp/cardiology.pdf"},
                    {"source": "notes", "title": "renal.pdf", "location": "p. 9", "local_path": "/tmp/renal.pdf"},
                ],
            )

            print("hybrid ranking & single-embed cache")
            embed_calls = 0
            rag._embed_cache.clear()
            hits = store.search_all("which ticket covers the audit write path?", sources={"jira", "notes"})
            check("embeds query only ONCE across multiple collections", embed_calls == 1, f"calls={embed_calls}")
            check("write-path ticket ranks above read-path via BM25+boost",
                  bool(hits) and hits[0].metadata.get("key") == "PROJ-202",
                  hits[0].citation() if hits else "none")
            check("citation carries validated https url",
                  hits[0].citation_meta().get("url") == "https://example.atlassian.net/browse/PROJ-202")

            # Repeat same query -> LRU cache hit -> 0 embed calls!
            hits2 = store.search_all("Which ticket covers the audit write path?", sources={"jira", "notes"})
            check("LRU cache avoids re-embedding identical normalised query", embed_calls == 1 and len(hits2) > 0)

            print("exact identifier pinning")
            pinned = store.search_all("status of PROJ-101?", sources={"jira"})
            check("exact ticket key PROJ-101 pinned to #1",
                  bool(pinned) and pinned[0].metadata.get("key") == "PROJ-101")

            print("BM25-only fallback (no embedding provider)")
            def no_embed(_texts):
                raise ProviderError("no embedding model")

            providers.embed = no_embed
            rag._embed_cache.clear()
            store.add(
                config.ANKI_COLLECTION,
                ["anki:1"],
                ["Spironolactone is an aldosterone antagonist used in heart failure."],
                [{"source": "anki", "deck": "Pharmacology", "front": "Spironolactone mechanism"}],
            )
            anki_hits = store.search_all("spironolactone aldosterone mechanism", sources={"anki"})
            check("BM25-only indexing & retrieval succeeds without embeddings",
                  len(anki_hits) == 1 and "Pharmacology" in anki_hits[0].citation(),
                  str([c.citation() for c in anki_hits]))

            print("stale pruning")
            removed = store.delete_stale(config.JIRA_COLLECTION, {"PROJ-202"})
            check("delete_stale removes unkept items", removed == 1 and store.count(config.JIRA_COLLECTION) == 1)

            # Punctuation torture test for FTS5 query sanitizer
            vs = VectorStore(Path(tmp) / "vectors.sqlite3")
            check("FTS5 handles punctuation safely", isinstance(vs.fts_query(config.ANKI_COLLECTION, 'C++ "quote" (test): PROJ-1?'), list))
            vs.close()
        finally:
            providers.embed = saved_embed
            config.CHROMA_DIR = saved_chroma
            rag._embed_cache.clear()

    print(f"\n{'PASS' if not failures else f'FAIL ({len(failures)})'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
