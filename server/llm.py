"""Generation routing by task, on top of server/providers."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from . import config, providers

log = logging.getLogger(__name__)


def backend() -> str:
    """'foundation' when the on-device model is usable, else the provider kind."""
    if providers.foundation().available():
        return "foundation"
    resolution = providers.resolve()
    return resolution.provider.kind if resolution.provider else "none"


def _use_foundation(task: str) -> bool:
    chosen = config.LIVE_BACKEND if task == "live" else config.SUMMARY_BACKEND
    return chosen == "foundation" and providers.foundation().available()


def stream(
    system: str,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float = 0.1,
    task: str = "live",
) -> Iterator[str]:
    """Streams an answer or summary token-by-token."""
    if _use_foundation(task):
        yield from providers.foundation().chat_stream("", system, prompt, num_predict=max_tokens)
        return
    num_ctx = config.LIVE_NUM_CTX if task == "live" else config.SUMMARY_NUM_CTX
    yield from providers.chat_stream(
        system, prompt, max_tokens=max_tokens, temperature=temperature, num_ctx=num_ctx
    )


def complete(
    system: str, prompt: str, *, max_tokens: int, temperature: float = 0.1,
    timeout: float = 240.0, task: str = "summary",
) -> str:
    """Non-streaming completion routed by task."""
    if _use_foundation(task):
        return providers.foundation().chat("", system, prompt, num_predict=max_tokens)

    model = None
    num_ctx = config.LIVE_NUM_CTX
    if task != "live":
        num_ctx = config.SUMMARY_NUM_CTX
        resolution = providers.resolve()
        if resolution.provider and config.SUMMARY_MODEL in resolution.provider.chat_models():
            model = config.SUMMARY_MODEL
    return providers.chat(
        system, prompt, max_tokens=max_tokens, temperature=temperature,
        num_ctx=num_ctx, timeout=timeout, model=model,
    )
