"""One-off migration: Chroma collections -> local SQLite vector store.

Vectors are copied verbatim, so no re-embedding is needed (that would be ~8
minutes of Ollama calls and would produce slightly different vectors). Run once
after switching to server/vectorstore.py.
"""

from __future__ import annotations

import logging
import sys

import chromadb
from chromadb.config import Settings

from server import config
from server.vectorstore import VectorStore

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("migrate")

COLLECTIONS = (
    config.JIRA_COLLECTION,
    config.CONFLUENCE_COLLECTION,
    config.CODE_COLLECTION,
)
BATCH = 500


def main() -> int:
    client = chromadb.PersistentClient(
        path=str(config.CHROMA_DIR), settings=Settings(anonymized_telemetry=False)
    )
    store = VectorStore(config.CHROMA_DIR / "vectors.sqlite3")

    total = 0
    for name in COLLECTIONS:
        try:
            collection = client.get_collection(name)
        except Exception:
            log.info("%s: not present in Chroma, skipping", name)
            continue

        size = collection.count()
        if size == 0:
            log.info("%s: empty, skipping", name)
            continue

        copied = 0
        offset = 0
        while offset < size:
            batch = collection.get(
                include=["documents", "metadatas", "embeddings"],
                limit=BATCH,
                offset=offset,
            )
            ids = batch.get("ids") or []
            if not ids:
                break
            embeddings = batch.get("embeddings")
            if embeddings is None:
                log.warning("%s: batch has no embeddings, skipping", name)
                break
            store.add(
                name,
                ids,
                batch.get("documents") or [],
                batch.get("metadatas") or [{} for _ in ids],
                [list(map(float, v)) for v in embeddings],
            )
            copied += len(ids)
            offset += BATCH

        log.info("%s: migrated %d/%d", name, copied, size)
        total += copied

    for name in COLLECTIONS:
        log.info("verify %s -> %d rows", name, store.count(name))
    log.info("migrated %d vectors total", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
