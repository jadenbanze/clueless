"""Local retrieval over Jira issues and repository code.

Embeddings are computed by Ollama (`nomic-embed-text`) and stored in a local
Chroma instance, so no text leaves the machine. Vectors are supplied explicitly
rather than through Chroma's embedding_function, which keeps the model choice in
one place.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import config, ollama_client

log = logging.getLogger(__name__)

# Ignored by the lexical boost: too common to distinguish anything.
_STOPWORDS = {
    "what", "which", "when", "where", "who", "whom", "this", "that", "these",
    "those", "with", "from", "have", "has", "had", "does", "doing", "about",
    "there", "their", "them", "they", "will", "would", "could", "should",
    "status", "ticket", "covers", "cover", "into", "over", "under", "been",
}


@dataclass
class Chunk:
    text: str
    metadata: dict
    score: float = 0.0

    def citation(self) -> str:
        source = self.metadata.get("source")
        if source == "jira":
            return f"{self.metadata.get('key', '?')} — {self.metadata.get('summary', '')}"
        if source == "confluence":
            return f"{self.metadata.get('space', '?')}: {self.metadata.get('title', '?')}"
        return f"{self.metadata.get('repo', '?')}/{self.metadata.get('path', '?')}"


class Store:
    """Thin wrapper around the two Chroma collections."""

    def __init__(self) -> None:
        import chromadb
        from chromadb.config import Settings

        config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        # Telemetry off, deliberately. Chroma defaults to shipping anonymised
        # usage data to PostHog; this tool's entire premise is that meeting
        # content stays on the machine, and the retry backoff on a blocked
        # network also added seconds to every collection call.
        self._client = chromadb.PersistentClient(
            path=str(config.CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False, allow_reset=False),
        )

    def _collection(self, name: str):
        return self._client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"}
        )

    def add(self, collection: str, ids: list[str], texts: list[str], metadatas: list[dict]) -> None:
        if not ids:
            return
        vectors = ollama_client.embed(texts)
        self._collection(collection).upsert(
            ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas
        )
        log.info("indexed %d chunks into %s", len(ids), collection)

    def count(self, collection: str) -> int:
        try:
            return self._collection(collection).count()
        except Exception:
            return 0

    def search(
        self, collection: str, query: str, top_k: int | None = None,
        over_fetch: int | None = None,
    ) -> list[Chunk]:
        if not query.strip():
            return []
        try:
            collection_handle = self._collection(collection)
            size = collection_handle.count()
            if size == 0:
                return []
            # Over-fetch so duplicates and low-value files can be filtered
            # without starving the result set — but never ask for more than the
            # collection holds, which makes Chroma emit a warning on every call.
            requested = min(over_fetch or (top_k or config.RETRIEVE_TOP_K) * 4, size)
            vector = ollama_client.embed([query])[0]
            results = collection_handle.query(
                query_embeddings=[vector],
                n_results=requested,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as error:
            log.warning("retrieval from %s failed: %s", collection, error)
            return []

        chunks: list[Chunk] = []
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        for document, metadata, distance in zip(documents, metadatas, distances):
            metadata = metadata or {}
            path = str(metadata.get("path", "")).lower()
            if any(marker in path for marker in config.CODE_LOW_VALUE_MARKERS):
                continue
            # Cosine distance -> similarity, so higher is better.
            chunks.append(Chunk(text=document, metadata=metadata, score=1.0 - distance))

        # Keep only the best chunk per file. Several windows of one file crowd
        # out other sources while adding little: they overlap heavily.
        best_per_file: dict[str, Chunk] = {}
        for chunk in chunks:
            key = chunk.citation()
            if key not in best_per_file or chunk.score > best_per_file[key].score:
                best_per_file[key] = chunk

        # Store the boosted value on the chunk itself. Ranking has to survive
        # the merge in search_all, which sorts a combined list; leaving the raw
        # vector score here silently discarded the lexical boost and put
        # read-path tickets above write-path ones again.
        for chunk in best_per_file.values():
            chunk.score = self._boost(chunk, query)

        ordered = sorted(best_per_file.values(), key=lambda c: c.score, reverse=True)
        return ordered[: top_k or config.RETRIEVE_TOP_K]

    @staticmethod
    def _boost(chunk: Chunk, query: str) -> float:
        """Vector score plus a small lexical bonus.

        Embeddings are weak on short, distinctive tokens. Asking which ticket
        covers the audit *write* path returned read-path tickets, because "read"
        and "write" sit almost on top of each other in embedding space. Exact
        keyword presence breaks that tie, and the bonus is deliberately small so
        it re-orders near-equal candidates without overriding semantics.
        """
        terms = {
            word
            for word in re.findall(r"[a-z0-9]{4,}", query.lower())
            if word not in _STOPWORDS
        }
        if not terms:
            return chunk.score
        text = chunk.text.lower()
        matches = sum(1 for term in terms if term in text)
        return chunk.score + min(matches, 5) * 0.02

    def search_all(
        self, query: str, top_k: int | None = None, sources: set[str] | None = None
    ) -> list[Chunk]:
        limit = top_k or config.RETRIEVE_TOP_K
        enabled = sources if sources is not None else {"jira", "code"}

        # A question that explicitly asks about a ticket should search tickets.
        # Splitting the budget evenly meant "is there a Jira ticket about X"
        # returned a source directory instead of the tickets, even though
        # matching issues were indexed.
        lowered = query.lower()
        ticket_intent = any(
            word in lowered
            for word in ("ticket", "jira", "story", "epic", "sprint", "backlog", "assigned")
        )

        jira: list[Chunk] = []
        confluence: list[Chunk] = []
        code: list[Chunk] = []

        if "jira" in enabled:
            jira = self.search(
                config.JIRA_COLLECTION, query,
                top_k=limit if ticket_intent else max(2, limit // 2),
            )
        if "confluence" in enabled:
            confluence = self.search(
                config.CONFLUENCE_COLLECTION, query,
                top_k=max(2, limit // 2),
            )
        if "code" in enabled:
            code = self.search(
                config.CODE_COLLECTION, query,
                top_k=1 if ticket_intent else max(2, limit // 2),
            )

        combined = jira + confluence + code
        combined.sort(key=lambda c: c.score, reverse=True)
        return combined[:limit]


def compress_chunk(text: str, query: str, max_chars: int = 320) -> str:
    """Keeps only the sentences of a chunk that plausibly answer the query.

    Retrieval returns whole files or whole tickets; most of a chunk is unrelated
    to the question. Sending all of it is the single largest contributor to
    prompt length, and prompt evaluation dominates answer latency (~1.6s of a
    2.2s answer at ~700 tok/s). Extracting matching sentences cuts the context
    substantially while keeping the words that actually answer.

    Falls back to the head of the chunk when nothing matches, so a chunk is never
    silently emptied.
    """
    body = text.strip()
    if len(body) <= max_chars:
        return body

    terms = {w for w in re.findall(r"[a-z0-9]{4,}", query.lower()) if w not in _STOPWORDS}
    sentences = re.split(r"(?<=[.!?\n])\s+", body)

    scored: list[tuple[int, int, str]] = []
    for index, sentence in enumerate(sentences):
        lowered = sentence.lower()
        score = sum(1 for term in terms if term in lowered)
        if score:
            scored.append((score, -index, sentence.strip()))

    if not scored:
        return body[:max_chars].rstrip() + "…"

    scored.sort(reverse=True)
    kept: list[str] = []
    used = 0
    for _score, _order, sentence in scored:
        if not sentence:
            continue
        if used + len(sentence) > max_chars:
            continue
        kept.append(sentence)
        used += len(sentence)
        if used >= max_chars * 0.8:
            break

    return " ".join(kept) if kept else body[:max_chars].rstrip() + "…"


def format_context(chunks: list[Chunk], query: str = "") -> str:
    """Renders retrieved chunks for the prompt, each with a citable label.

    Chunks are truncated per-item and overall. Code chunks are 120 lines, and
    feeding several untruncated ones swamps the instruction budget — the model
    stops following the brevity rules and prompt processing gets slow enough to
    make a hotkey answer take 15s instead of 2s.
    """
    if not chunks:
        return "(no matching context found)"

    blocks = []
    used = 0
    for chunk in chunks:
        # Extract the sentences that match the question before truncating. Doing
        # it the other way round keeps whichever 800 characters happen to come
        # first, which is usually the least relevant part of a file.
        text = compress_chunk(chunk.text, query, config.CONTEXT_MAX_CHARS_PER_CHUNK)
        block = f"[{chunk.citation()}]\n{text}"
        if used + len(block) > config.CONTEXT_MAX_TOTAL_CHARS:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks) if blocks else "(no matching context found)"
