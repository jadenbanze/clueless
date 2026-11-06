"""Provider interface shared by every local model server."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

# Substrings that mark a model as an embedding model rather than a chat model.
EMBED_MARKERS = ("embed", "bge-", "minilm", "e5-", "gte-")

# Last request's timings in milliseconds, filled by whichever provider answered.
# The only reliable way to tell whether latency lives in prompt processing or
# generation — which decides whether to shorten the prompt or cap the output.
last_timings: dict = {}


class ProviderError(RuntimeError):
    pass


def is_embedding_model(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in EMBED_MARKERS)


class Provider(Protocol):
    id: str
    kind: str
    label: str
    base_url: str

    def available(self) -> bool: ...

    def models(self) -> list[str]: ...

    def chat_models(self) -> list[str]: ...

    def largest_chat_model(self) -> str | None: ...

    def chat(self, model: str, system: str, user: str, *, num_ctx: int, num_predict: int,
             temperature: float, timeout: float = 90.0) -> str: ...

    def chat_stream(self, model: str, system: str, user: str, *, num_ctx: int, num_predict: int,
                    temperature: float, timeout: float = 120.0) -> Iterator[str]: ...

    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    def warm(self, model: str, embed_model: str | None, num_ctx: int) -> dict: ...
