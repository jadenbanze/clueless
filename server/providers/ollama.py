"""Ollama (/api/chat, /api/embed, /api/tags)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator

from .. import config
from . import http
from .base import ProviderError, is_embedding_model, last_timings

log = logging.getLogger(__name__)

# nomic-embed-text has a limited context; oversized chunks make Ollama return
# HTTP 500 rather than a useful error, so inputs are clipped before sending.
EMBED_MAX_CHARS = 6000


def _total_ram_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 16 * 1024**3


def _record_timings(data: dict) -> None:
    def ms(key: str) -> float:
        return round((data.get(key) or 0) / 1_000_000, 1)

    last_timings.clear()
    last_timings.update({
        "load_ms": ms("load_duration"),
        "prompt_tokens": data.get("prompt_eval_count") or 0,
        "prompt_ms": ms("prompt_eval_duration"),
        "output_tokens": data.get("eval_count") or 0,
        "output_ms": ms("eval_duration"),
        "total_ms": ms("total_duration"),
    })
    if last_timings["prompt_tokens"]:
        last_timings["prompt_tok_per_s"] = round(
            last_timings["prompt_tokens"] / max(last_timings["prompt_ms"], 1) * 1000, 1)
    if last_timings["output_tokens"]:
        last_timings["output_tok_per_s"] = round(
            last_timings["output_tokens"] / max(last_timings["output_ms"], 1) * 1000, 1)


class OllamaProvider:
    kind = "ollama"

    def __init__(self, base_url: str = config.OLLAMA_HOST, provider_id: str = "ollama", label: str = "Ollama"):
        self.base_url = base_url.rstrip("/")
        self.id = provider_id
        self.label = label

    def available(self) -> bool:
        return http.reachable(f"{self.base_url}/api/tags")

    def _tags(self) -> list[dict]:
        try:
            return http.get_json(f"{self.base_url}/api/tags", timeout=5).get("models", [])
        except (ProviderError, json.JSONDecodeError):
            return []

    def models(self) -> list[str]:
        return [m["name"] for m in self._tags()]

    def chat_models(self) -> list[str]:
        return [m["name"] for m in self._tags() if not is_embedding_model(m["name"])]

    def largest_chat_model(self) -> str | None:
        # Larger usually means more capable at multi-rule grounding, but only up
        # to what fits: a 42 GB model on a 36 GB Mac thrashes swap and answers
        # in minutes. Half of RAM leaves room for the app and the KV cache.
        budget = _total_ram_bytes() * 0.5
        candidates = [m for m in self._tags()
                      if not is_embedding_model(m["name"]) and (m.get("size") or 0) <= budget]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.get("size") or 0)["name"]

    def _payload(self, model, system, user, num_ctx, num_predict, temperature, stream):
        return {
            "model": model,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "stream": stream,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": temperature},
        }

    def chat(self, model, system, user, *, num_ctx=8192, num_predict=320, temperature=0.2, timeout=90.0) -> str:
        data = http.post_json(f"{self.base_url}/api/chat",
                              self._payload(model, system, user, num_ctx, num_predict, temperature, False), timeout)
        _record_timings(data)
        return ((data.get("message") or {}).get("content") or "").strip()

    def chat_stream(self, model, system, user, *, num_ctx=8192, num_predict=320, temperature=0.2,
                    timeout=120.0) -> Iterator[str]:
        payload = self._payload(model, system, user, num_ctx, num_predict, temperature, True)
        for line in http.post_lines(f"{self.base_url}/api/chat", payload, timeout):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            piece = (event.get("message") or {}).get("content") or ""
            if piece:
                yield piece
            if event.get("done"):
                _record_timings(event)
                return

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Uses /api/embed (not the legacy /api/embeddings, which 500s on 0.34+)."""
        if not texts:
            return []
        clipped = [text[:EMBED_MAX_CHARS] for text in texts]
        data = http.post_json(f"{self.base_url}/api/embed", {"model": model, "input": clipped}, timeout=180)
        vectors = data.get("embeddings")
        if not vectors or len(vectors) != len(clipped):
            raise ProviderError(f"expected {len(clipped)} embeddings from {model}, got {len(vectors or [])}")
        return vectors

    def warm(self, model: str, embed_model: str | None, num_ctx: int) -> dict:
        """Loads models so the first question is not cold (measured: 3.3s of load otherwise)."""
        result = {}
        jobs = [("chat", f"{self.base_url}/api/chat", {
            "model": model, "messages": [{"role": "user", "content": "ok"}], "stream": False,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            # Must match live requests: a different num_ctx triggers a reload.
            "options": {"num_ctx": num_ctx, "num_predict": 1}})]
        if embed_model:
            jobs.append(("embed", f"{self.base_url}/api/embed", {"model": embed_model, "input": ["warm"]}))
        for label, url, body in jobs:
            try:
                http.post_json(url, body, timeout=180)
                result[label] = "warm"
            except ProviderError as error:
                result[label] = f"failed: {error}"
        return result
