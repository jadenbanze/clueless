"""Minimal local vector store: SQLite for persistence, numpy for search.

Replaces ChromaDB, which cost ~360 MB of dependencies for functionality this
project does not use: an HTTP client, a distributed backend, telemetry, and an
ONNX embedder. Our usage is narrow — upsert vectors, count, and a cosine top-k
over a few thousand chunks, which is 16 MB of floats and trivial to brute-force.

Embeddings are normalised on write, so cosine similarity is a dot product.

Scale note: this is linear in collection size. At ~5k chunks a query is a few
milliseconds. If a collection ever reaches millions of rows this needs an ANN
index (hnswlib or similar); that is a deliberate, documented trade.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


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
        self._connection.commit()
        # In-memory cache of (ids, matrix, texts, metadatas) per collection.
        self._cache: dict[str, tuple[list[str], np.ndarray, list[str], list[dict]]] = {}

    # ---------------------------------------------------------------- write

    def add(
        self,
        collection: str,
        ids: list[str],
        texts: list[str],
        metadatas: list[dict],
        embeddings: list[list[float]],
    ) -> None:
        if not ids:
            return

        rows = []
        for item_id, text, metadata, vector in zip(ids, texts, metadatas, embeddings):
            array = np.asarray(vector, dtype=np.float32)
            norm = float(np.linalg.norm(array))
            if norm > 0:
                array = array / norm
            rows.append(
                (collection, item_id, text, json.dumps(metadata or {}), array.tobytes())
            )

        with self._lock:
            self._connection.executemany(
                "INSERT OR REPLACE INTO items (collection, id, text, metadata, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._connection.commit()
            self._cache.pop(collection, None)

    # ---------------------------------------------------------------- read

    def count(self, collection: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM items WHERE collection = ?", (collection,)
            ).fetchone()
        return int(row[0]) if row else 0

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
            empty = ([], np.zeros((0, 0), dtype=np.float32), [], [])
            self._cache[collection] = empty
            return empty

        ids = [row[0] for row in rows]
        texts = [row[1] for row in rows]
        metadatas = [json.loads(row[2]) for row in rows]
        matrix = np.vstack([np.frombuffer(row[3], dtype=np.float32) for row in rows])

        loaded = (ids, matrix, texts, metadatas)
        self._cache[collection] = loaded
        return loaded

    def query(
        self, collection: str, embedding: list[float], n_results: int
    ) -> list[tuple[str, dict, float]]:
        """Returns (text, metadata, similarity) ordered by similarity, best first."""
        ids, matrix, texts, metadatas = self._load(collection)
        if not ids:
            return []

        query_vector = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(query_vector))
        if norm > 0:
            query_vector = query_vector / norm

        # Both sides are unit vectors, so the dot product is cosine similarity.
        scores = matrix @ query_vector
        top = np.argsort(-scores)[: max(1, min(n_results, len(scores)))]

        return [(texts[i], metadatas[i], float(scores[i])) for i in top]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
