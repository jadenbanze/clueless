"""Local hybrid retrieval (dense vectors + SQLite FTS5 BM25 + RRF) across sources.

Key properties:
  * Query embeddings are computed at most ONCE per ``search_all()`` call and
    cached in a bounded thread-safe LRU cache (eliminating redundant loopback
    HTTP round-trips across collections and speculative retries).
  * If no embedding model is available (for example, Apple Intelligence-only
    setups), indexing and search degrade cleanly to SQLite FTS5 BM25 keyword
    retrieval instead of failing completely.
  * Exact identifiers in the question (ticket keys like ``PROJ-123``, ``PMID``s,
    or ``PR #42``) are pinned deterministically alongside semantic matches.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

from . import config, providers
from .providers.base import ProviderError
from .vectorstore import VectorStore

log = logging.getLogger(__name__)

_STOPWORDS = {
    "what", "which", "when", "where", "who", "whom", "this", "that", "these",
    "those", "with", "from", "have", "has", "had", "does", "doing", "about",
    "there", "their", "them", "they", "will", "would", "could", "should",
    "status", "ticket", "covers", "cover", "into", "over", "under", "been",
}

_EXACT_ID_PATTERNS = (
    re.compile(r"\b([A-Z]{2,}[A-Z0-9_]*-\d+)\b"),
    re.compile(r"\bPMID[:\s]*(\d{5,10})\b", re.IGNORECASE),
    re.compile(r"\b(?:PR|pull request)\s*#?(\d{1,6})\b", re.IGNORECASE),
)

_EMBED_CACHE_MAX = 64
_embed_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_embed_lock = threading.Lock()


def _cached_query_embedding(query: str) -> list[float] | None:
    norm = " ".join(query.lower().split())
    if not norm:
        return None
    model_key = config.EMBED_MODEL
    cache_key = (model_key, norm)
    with _embed_lock:
        hit = _embed_cache.get(cache_key)
        if hit is not None:
            _embed_cache.move_to_end(cache_key)
            return hit

    try:
        vector = providers.embed([query])[0]
    except Exception as error:
        log.debug("query embedding unavailable (%s); using FTS5 BM25 fallback", error)
        return None

    with _embed_lock:
        _embed_cache[cache_key] = vector
        if len(_embed_cache) > _EMBED_CACHE_MAX:
            _embed_cache.popitem(last=False)
    return vector


def extract_identifiers(text: str) -> list[str]:
    found: list[str] = []
    if not text:
        return found
    for key in _EXACT_ID_PATTERNS[0].findall(text):
        found.append(key)
    for pmid in _EXACT_ID_PATTERNS[1].findall(text):
        found.append(pmid)
        found.append(f"pmid:{pmid}")
    for pr_num in _EXACT_ID_PATTERNS[2].findall(text):
        found.append(f"pr#{pr_num}")
    return found


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
            section = self.metadata.get("section")
            title = self.metadata.get("title", "?")
            suffix = f" › {section}" if section and section != title else ""
            return f"{self.metadata.get('space', '?')}: {title}{suffix}"
        if source == "code":
            if self.metadata.get("kind") == "pr":
                return f"{self.metadata.get('repo', '?')} PR #{self.metadata.get('pr_number', '?')} — {self.metadata.get('title', '')}"
            return f"{self.metadata.get('repo', '?')}/{self.metadata.get('path', '?')}"
        if source == "notes":
            loc = self.metadata.get("location", "")
            name = self.metadata.get("title") or self.metadata.get("path", "note")
            return f"{name}" + (f" ({loc})" if loc else "")
        if source == "anki":
            deck = self.metadata.get("deck", "Anki")
            front = self.metadata.get("front", "")[:48]
            return f"{deck}: {front}" if front else deck
        if source == "pubmed":
            pmid = self.metadata.get("pmid")
            cite_key = f"PMID:{pmid}" if pmid else self.metadata.get("key", "Paper")
            return f"{cite_key} — {self.metadata.get('title', '')[:60]}"
        if source == "canvas":
            return f"{self.metadata.get('course', 'Course')}: {self.metadata.get('title', '?')}"
        if source == "notion":
            return f"Notion: {self.metadata.get('title', 'Untitled')}"
        return str(self.metadata.get("title") or self.metadata.get("path") or "source")

    def citation_meta(self) -> dict:
        out = {"label": self.citation()}
        url = self.metadata.get("url")
        local_path = self.metadata.get("local_path")
        if isinstance(url, str) and url.startswith("https://"):
            out["url"] = url
        elif isinstance(local_path, str) and local_path.startswith("/"):
            out["path"] = local_path
        return out


class Store:
    """Hybrid vector + BM25 retrieval over the local SQLite store."""

    def __init__(self) -> None:
        config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        self._store = VectorStore(config.CHROMA_DIR / "vectors.sqlite3")

    def add(
        self,
        collection: str,
        ids: list[str],
        texts: list[str],
        metadatas: list[dict],
    ) -> None:
        if not ids:
            return
        vectors: list[list[float]] | None = None
        try:
            vectors = providers.embed(texts)
        except ProviderError as error:
            log.info("indexing %d chunks into %s in BM25-only mode (%s)", len(ids), collection, error)
        self._store.add(collection, ids, texts, metadatas, vectors)
        log.info("indexed %d chunks into %s", len(ids), collection)

    def delete_stale(self, collection: str, keep_ids: set[str]) -> int:
        return self._store.delete_stale(collection, keep_ids)

    def count(self, collection: str) -> int:
        return self._store.count(collection)

    def counts(self) -> dict[str, int]:
        return self._store.counts()

    def search(
        self,
        collection: str,
        query: str,
        top_k: int | None = None,
        over_fetch: int | None = None,
        query_vector: list[float] | None = None,
        _skip_embed: bool = False,
    ) -> list[Chunk]:
        if not query.strip():
            return []
        try:
            size = self._store.count(collection)
            if size == 0:
                return []
            limit = top_k or config.RETRIEVE_TOP_K
            requested = min(over_fetch or limit * 4, size)

            vector = query_vector
            if vector is None and not _skip_embed:
                vector = _cached_query_embedding(query)

            vec_rows = self._store.query_with_ids(collection, vector, requested) if vector else []
            fts_rows = self._store.fts_query(collection, query, requested)
            pinned = self._store.exact_lookup(collection, extract_identifiers(query))
        except Exception as error:
            log.warning("retrieval from %s failed: %s", collection, error)
            return []

        # Reciprocal Rank Fusion (RRF) on vector similarity + FTS5 BM25 ranks,
        # anchored to vector cosine scale (~0.55–0.95) so cross-collection
        # sorting in search_all remains well-calibrated.
        candidates: dict[str, Chunk] = {}
        rrf_bonus: dict[str, float] = {}

        for rank, (item_id, document, metadata, similarity) in enumerate(vec_rows):
            metadata = metadata or {}
            path = str(metadata.get("path", "")).lower()
            if any(marker in path for marker in config.CODE_LOW_VALUE_MARKERS):
                continue
            candidates[item_id] = Chunk(text=document, metadata=metadata, score=similarity)
            rrf_bonus[item_id] = rrf_bonus.get(item_id, 0.0) + 0.15 / (10 + rank)

        for rank, (item_id, document, metadata, _bm25) in enumerate(fts_rows):
            metadata = metadata or {}
            path = str(metadata.get("path", "")).lower()
            if any(marker in path for marker in config.CODE_LOW_VALUE_MARKERS):
                continue
            if item_id not in candidates:
                # BM25-only hit (either outside vector top-N or BM25-only index).
                base = 0.62 if vec_rows else 0.75
                candidates[item_id] = Chunk(text=document, metadata=metadata, score=base)
            rrf_bonus[item_id] = rrf_bonus.get(item_id, 0.0) + 0.18 / (8 + rank)

        for item_id, document, metadata in pinned:
            metadata = metadata or {}
            existing = candidates.get(item_id)
            score = max(existing.score if existing else 0.0, 0.96)
            candidates[item_id] = Chunk(text=document, metadata=metadata, score=score)
            rrf_bonus[item_id] = rrf_bonus.get(item_id, 0.0) + 0.25

        for item_id, chunk in candidates.items():
            chunk.score += rrf_bonus.get(item_id, 0.0)

        best_per_file: dict[str, Chunk] = {}
        for chunk in candidates.values():
            key = chunk.citation()
            if key not in best_per_file or chunk.score > best_per_file[key].score:
                best_per_file[key] = chunk

        for chunk in best_per_file.values():
            chunk.score = self._boost(chunk, query)

        ordered = sorted(best_per_file.values(), key=lambda c: c.score, reverse=True)
        return ordered[:limit]

    @staticmethod
    def _boost(chunk: Chunk, query: str) -> float:
        terms = {
            word
            for word in re.findall(r"[a-z0-9]{3,}", query.lower())
            if len(word) >= 4 and word not in _STOPWORDS
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
        active_sources = [src for src in enabled if src in config.SOURCE_COLLECTIONS]
        if not active_sources or not query.strip():
            return []

        # Filter to collections that actually contain indexed items before
        # paying for a loopback embedding call.
        populated = [
            (src, config.SOURCE_COLLECTIONS[src])
            for src in active_sources
            if self._store.count(config.SOURCE_COLLECTIONS[src]) > 0
        ]
        if not populated:
            return []

        # Compute query vector ONCE across all enabled collections.
        query_vec = _cached_query_embedding(query)

        lowered = query.lower()
        ticket_intent = any(
            word in lowered
            for word in ("ticket", "jira", "story", "epic", "sprint", "backlog", "assigned")
        )
        pr_intent = any(
            word in lowered for word in ("pull request", " pr ", "merged", "review", "branch", "build")
        )

        combined: list[Chunk] = []
        for src, collection in populated:
            per_source_k = max(2, limit // 2)
            if src == "jira" and ticket_intent:
                per_source_k = limit
            elif src == "code" and ticket_intent and not pr_intent:
                per_source_k = 1
            elif len(populated) == 1:
                per_source_k = limit
            combined.extend(
                self.search(
                    collection,
                    query,
                    top_k=per_source_k,
                    query_vector=query_vec,
                    _skip_embed=True,
                )
            )

        combined.sort(key=lambda c: c.score, reverse=True)
        return combined[:limit]


def compress_chunk(text: str, query: str, max_chars: int = 320) -> str:
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
    if not chunks:
        return "(no matching context found)"

    blocks = []
    used = 0
    for chunk in chunks:
        text = compress_chunk(chunk.text, query, config.CONTEXT_MAX_CHARS_PER_CHUNK)
        block = f"[{chunk.citation()}]\n{text}"
        if used + len(block) > config.CONTEXT_MAX_TOTAL_CHARS:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks) if blocks else "(no matching context found)"
