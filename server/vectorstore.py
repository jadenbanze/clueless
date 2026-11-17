"""Minimal local hybrid store: SQLite + FTS5 (BM25) + numpy cosine search.

Replaces ChromaDB (~360 MB of dependencies) with two tables in one SQLite file:
  * ``items``     — source text, JSON metadata, and unit-normalised float32 vectors
  * ``items_fts`` — SQLite FTS5 index for fast BM25 keyword retrieval, which also
                    keeps local search working when no embedding model is running.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_FTS_TOKEN = re.compile(r"[A-Za-z0-9]{2,}")


class VectorStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                collection TEXT NOT NULL,
                id         TEXT NOT NULL,
                text       TEXT NOT NULL,
                metadata   TEXT NOT NULL,
                embedding  BLOB NOT NULL,
                PRIMARY KEY (collection, id)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_collection ON items(collection)"
        )
        self._has_fts = False
        try:
            self._connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
                    collection,
                    item_id UNINDEXED,
                    text,
                    tokenize='porter unicode61'
                )
                """
            )
            self._has_fts = True
            # Backfill existing databases created before FTS5 was added.
            item_count = self._connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            fts_count = self._connection.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0]
            if item_count > 0 and fts_count == 0:
                self._connection.execute(
                    "INSERT INTO items_fts(rowid, collection, item_id, text) "
                    "SELECT rowid, collection, id, text FROM items"
                )
        except sqlite3.OperationalError as error:
            log.warning("SQLite FTS5 unavailable (%s); falling back to vector-only search", error)
        self._connection.commit()
        # In-memory cache of (ids, matrix, has_vectors, texts, metadatas) per collection.
        self._cache: dict[str, tuple[list[str], np.ndarray, bool, list[str], list[dict]]] = {}

    # ---------------------------------------------------------------- write

    def add(
        self,
        collection: str,
        ids: list[str],
        texts: list[str],
        metadatas: list[dict],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        if not ids:
            return

        vectors = embeddings if embeddings is not None else [[] for _ in ids]
        rows = []
        for item_id, text, metadata, vector in zip(ids, texts, metadatas, vectors, strict=True):
            if vector:
                array = np.asarray(vector, dtype=np.float32)
                norm = float(np.linalg.norm(array))
                if norm > 0:
                    array = array / norm
                blob = array.tobytes()
            else:
                blob = b""
            rows.append((collection, item_id, text, json.dumps(metadata or {}), blob))

        with self._lock:
            if self._has_fts:
                existing_rowids = [
                    (r[0],)
                    for item_id in ids
                    for r in self._connection.execute(
                        "SELECT rowid FROM items WHERE collection = ? AND id = ?",
                        (collection, item_id),
                    ).fetchall()
                ]
                if existing_rowids:
                    self._connection.executemany(
                        "DELETE FROM items_fts WHERE rowid = ?",
                        existing_rowids,
                    )
            self._connection.executemany(
                "INSERT OR REPLACE INTO items (collection, id, text, metadata, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            if self._has_fts:
                self._connection.executemany(
                    "INSERT INTO items_fts(rowid, collection, item_id, text) "
                    "SELECT rowid, collection, id, text FROM items "
                    "WHERE collection = ? AND id = ?",
                    [(collection, item_id) for item_id in ids],
                )
            self._connection.commit()
            self._cache.pop(collection, None)

    def delete_stale(self, collection: str, keep_ids: set[str]) -> int:
        """Removes items in ``collection`` whose id is not in ``keep_ids``."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT rowid, id FROM items WHERE collection = ?", (collection,)
            ).fetchall()
            drop_pairs = [(rowid,) for rowid, item_id in rows if item_id not in keep_ids]
            if not drop_pairs:
                return 0
            if self._has_fts:
                self._connection.executemany("DELETE FROM items_fts WHERE rowid = ?", drop_pairs)
            self._connection.executemany("DELETE FROM items WHERE rowid = ?", drop_pairs)
            self._connection.commit()
            self._cache.pop(collection, None)
            return len(drop_pairs)

    # ---------------------------------------------------------------- read

    def count(self, collection: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM items WHERE collection = ?", (collection,)
            ).fetchone()
        return int(row[0]) if row else 0

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT collection, COUNT(*) FROM items GROUP BY collection"
            ).fetchall()
        return {str(col): int(cnt) for col, cnt in rows}

    def _load(self, collection: str):
        cached = self._cache.get(collection)
        if cached is not None:
            return cached

        with self._lock:
            rows = self._connection.execute(
                "SELECT id, text, metadata, embedding FROM items WHERE collection = ?",
                (collection,),
            ).fetchall()

        if not rows:
            empty = ([], np.zeros((0, 0), dtype=np.float32), False, [], [])
            self._cache[collection] = empty
            return empty

        ids = [row[0] for row in rows]
        texts = [row[1] for row in rows]
        metadatas = [json.loads(row[2]) for row in rows]

        # Support collections indexed in BM25-only mode (empty embedding blobs)
        # or mixed vector dimensions cleanly.
        dims = [len(row[3]) // 4 for row in rows if row[3]]
        target_dim = dims[0] if dims else 0
        has_vectors = target_dim > 0 and all(len(row[3]) == target_dim * 4 for row in rows)
        if has_vectors:
            matrix = np.vstack([np.frombuffer(row[3], dtype=np.float32) for row in rows])
        else:
            matrix = np.zeros((0, 0), dtype=np.float32)

        loaded = (ids, matrix, has_vectors, texts, metadatas)
        self._cache[collection] = loaded
        return loaded

    def query(
        self, collection: str, embedding: list[float], n_results: int
    ) -> list[tuple[str, dict, float]]:
        """Returns (text, metadata, similarity) ordered by cosine similarity."""
        rows = self.query_with_ids(collection, embedding, n_results)
        return [(text, meta, sim) for _id, text, meta, sim in rows]

    def query_with_ids(
        self, collection: str, embedding: list[float], n_results: int
    ) -> list[tuple[str, str, dict, float]]:
        ids, matrix, has_vectors, texts, metadatas = self._load(collection)
        if not ids or not has_vectors or not embedding:
            return []

        query_vector = np.asarray(embedding, dtype=np.float32)
        if matrix.shape[1] != query_vector.shape[0]:
            return []
        norm = float(np.linalg.norm(query_vector))
        if norm > 0:
            query_vector = query_vector / norm

        scores = matrix @ query_vector
        top = np.argsort(-scores)[: max(1, min(n_results, len(scores)))]
        return [(ids[i], texts[i], metadatas[i], float(scores[i])) for i in top]

    def fts_query(
        self, collection: str, query_text: str, n_results: int = 16
    ) -> list[tuple[str, str, dict, float]]:
        """BM25 keyword search via SQLite FTS5. Safe on arbitrary punctuation."""
        if not self._has_fts or not query_text.strip():
            return []
        tokens = []
        seen = set()
        for tok in _FTS_TOKEN.findall(query_text):
            low = tok.lower()
            if low not in seen:
                seen.add(low)
                tokens.append(f'"{low}"')
        if not tokens:
            return []

        match_expr = f'collection : "{collection}" AND (' + " OR ".join(tokens[:16]) + ")"
        try:
            with self._lock:
                rows = self._connection.execute(
                    """
                    SELECT items.id, items.text, items.metadata, -bm25(items_fts) AS rank_score
                    FROM items_fts
                    JOIN items ON items.rowid = items_fts.rowid
                    WHERE items_fts MATCH ?
                    ORDER BY bm25(items_fts)
                    LIMIT ?
                    """,
                    (match_expr, max(1, n_results)),
                ).fetchall()
        except sqlite3.OperationalError:
            return []

        return [(r[0], r[1], json.loads(r[2]), float(r[3])) for r in rows]

    def exact_lookup(self, collection: str, keys: list[str]) -> list[tuple[str, str, dict]]:
        """Deterministic lookup for exact ticket IDs, PR numbers or PMIDs."""
        if not keys:
            return []
        ids, _matrix, _has_vec, texts, metadatas = self._load(collection)
        if not ids:
            return []
        wanted = {k.lower() for k in keys if k}
        found = []
        for item_id, text, meta in zip(ids, texts, metadatas, strict=True):
            candidates = {
                item_id.lower(),
                str(meta.get("key", "")).lower(),
                str(meta.get("pmid", "")).lower(),
                f"pr#{meta.get('pr_number')}".lower() if meta.get("pr_number") else "",
            }
            if wanted & candidates:
                found.append((item_id, text, meta))
        return found

    def close(self) -> None:
        with self._lock:
            self._connection.close()
