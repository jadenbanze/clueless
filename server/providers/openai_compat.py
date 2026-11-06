"""OpenAI-compatible servers: llama.cpp `llama-server`, LM Studio, Jan.

Uses /v1/models, /v1/chat/completions (SSE when streaming) and /v1/embeddings.
No API key is sent; these are local servers.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from . import http
from .base import ProviderError, is_embedding_model, last_timings


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(self, base_url: str, provider_id: str = "openai_compat", label: str = "OpenAI-compatible server"):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.id = provider_id
        self.label = label

    def available(self) -> bool:
        return http.reachable(f"{self.base_url}/v1/models")

    def models(self) -> list[str]:
        try:
            data = http.get_json(f"{self.base_url}/v1/models", timeout=5)
        except (ProviderError, json.JSONDecodeError):
            return []
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def chat_models(self) -> list[str]:
        return [m for m in self.models() if not is_embedding_model(m)]

    def largest_chat_model(self) -> str | None:
        # /v1/models carries no size; llama-server serves exactly one model, and
        # LM Studio lists the loaded one first.
        models = self.chat_models()
        return models[0] if models else None

    def _payload(self, model, system, user, num_predict, temperature, stream):
        return {
            "model": model,
            "stream": stream,
            "max_tokens": num_predict,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }

    def chat(self, model, system, user, *, num_ctx=8192, num_predict=320, temperature=0.2, timeout=90.0) -> str:
        started = time.monotonic()
        data = http.post_json(f"{self.base_url}/v1/chat/completions",
                              self._payload(model, system, user, num_predict, temperature, False), timeout)
        usage = data.get("usage") or {}
        last_timings.clear()
        last_timings.update({
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_ms": round((time.monotonic() - started) * 1000, 1),
        })
        choices = data.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()

    def chat_stream(self, model, system, user, *, num_ctx=8192, num_predict=320, temperature=0.2,
                    timeout=120.0) -> Iterator[str]:
        started = time.monotonic()
        payload = self._payload(model, system, user, num_predict, temperature, True)
        for line in http.post_lines(f"{self.base_url}/v1/chat/completions", payload, timeout):
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                event = json.loads(body)
            except json.JSONDecodeError:
                continue
            for choice in event.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    yield piece
        last_timings.clear()
        last_timings["total_ms"] = round((time.monotonic() - started) * 1000, 1)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = http.post_json(f"{self.base_url}/v1/embeddings",
                              {"model": model, "input": [t[:6000] for t in texts]}, timeout=180)
        rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or not all(vectors):
            raise ProviderError(f"expected {len(texts)} embeddings from {model}, got {len(vectors)}")
        return vectors

    def warm(self, model: str, embed_model: str | None, num_ctx: int) -> dict:
        try:
            self.chat(model, "Reply with one word.", "ok", num_predict=1, timeout=180)
            return {"chat": "warm"}
        except ProviderError as error:
            return {"chat": f"failed: {error}"}
