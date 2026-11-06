"""Generation routing by task, on top of server/providers.

Measured on the same grounded-question battery:

    Foundation Models   4/4 correct, avg  874ms, no process, 0 GB disk
    llama3.1:8b         5/5 correct, avg 3823ms, server + ~5 GB

Live answers go to the resolved local provider (an 8B-class model by default):
Foundation Models abstained on answerable questions and leaked unrelated
instructions, and a wrong answer mid-meeting costs more than the seconds saved.
Summaries prefer Foundation Models, which passed every structural and grounding
check at 2.8s versus ~7s. The provider ladder in server/providers falls back to
Foundation Models for live answers too when no server is running.
"""

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


def stream(system: str, prompt: str, *, max_tokens: int, temperature: float = 0.1) -> Iterator[str]:
    """Streams a live answer. Set LLM_BACKEND=foundation to prefer the on-device model."""
    if _use_foundation("live"):
        yield from providers.foundation().chat_stream("", system, prompt, num_predict=max_tokens)
        return
    yield from providers.chat_stream(system, prompt, max_tokens=max_tokens, temperature=temperature)


def complete(
    system: str, prompt: str, *, max_tokens: int, temperature: float = 0.1,
    timeout: float = 240.0, task: str = "summary",
) -> str:
    """Non-streaming completion.

    `task` selects the backend: "live" answers must use the accurate model,
    "summary" uses the faster one. Routing live answers through the summary
    backend by default silently degraded them — the non-streaming answer path
    went to Foundation Models and started abstaining on answerable questions.
    """
    if _use_foundation(task):
        return providers.foundation().chat("", system, prompt, num_predict=max_tokens)

    model = None
    num_ctx = config.LIVE_NUM_CTX
    if task != "live":
        num_ctx = config.SUMMARY_NUM_CTX
        resolution = providers.resolve()
        if resolution.provider and config.SUMMARY_MODEL in resolution.provider.chat_models():
            model = config.SUMMARY_MODEL
    return providers.chat(system, prompt, max_tokens=max_tokens, temperature=temperature,
                          num_ctx=num_ctx, timeout=timeout, model=model)
