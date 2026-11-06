"""Local model providers, auto-detection and the fallback ladder.

Resolution order for answers:

  1. The configured provider (or, under `auto`, the first local server that
     responds) with the configured model.
  2. Same provider, configured model missing: the largest installed chat model,
     reported as degraded with the substitution named.
  3. No server at all: Apple Intelligence on-device, reported as degraded
     because it was measured to be less reliable at grounded answers.
  4. Nothing: status "none" with fix-it steps for the UI.

Embeddings resolve separately; with no embedding model, retrieval (Jira,
Confluence, code) is disabled rather than failing per question.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from .. import config
from . import llamacpp
from .base import Provider, ProviderError, is_embedding_model, last_timings
from .foundation import FoundationProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "ProviderError", "Resolution", "chat", "chat_stream", "detect", "embed", "foundation",
    "last_timings", "resolve", "status", "test_answer", "warm",
]

log = logging.getLogger(__name__)

# Probed under `auto`, in order. Loopback only.
AUTO_CANDIDATES: list[tuple[str, str, str, str]] = [
    ("ollama", "ollama", "Ollama", config.OLLAMA_HOST),
    ("llamacpp", "openai_compat", "llama.cpp server", "http://127.0.0.1:8080"),
    ("llamacpp-8081", "openai_compat", "llama.cpp server", "http://127.0.0.1:8081"),
    ("lmstudio", "openai_compat", "LM Studio", "http://127.0.0.1:1234"),
]

_foundation = FoundationProvider()


def _build(provider_id: str, kind: str, label: str, url: str) -> Provider:
    if kind == "ollama":
        return OllamaProvider(url, provider_id, label)
    return OpenAICompatProvider(url, provider_id, label)


@dataclass
class Resolution:
    status: str                      # ok | degraded | none
    message: str = ""
    provider: Provider | None = None
    model: str | None = None
    embed_provider: Provider | None = None
    embed_model: str | None = None
    fixes: list[dict] = field(default_factory=list)
    resolved_at: float = field(default_factory=time.monotonic)

    def public(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "provider": self.provider.id if self.provider else None,
            "kind": self.provider.kind if self.provider else None,
            "label": self.provider.label if self.provider else None,
            "model": self.model,
            "embeddings": bool(self.embed_model),
            "embed_model": self.embed_model,
            "fixes": self.fixes,
        }


FIXES = [
    {"title": "Install Ollama", "command": "brew install ollama && ollama serve"},
    {"title": "Download a model", "command": f"ollama pull {config.LIVE_MODEL}"},
    {"title": "Or use a GGUF file", "command": "Settings → Model → Run a GGUF file with llama.cpp"},
]


def _configured_provider() -> Provider | None:
    """The explicitly configured provider, or None under `auto`."""
    choice = config.LLM_PROVIDER
    if choice == "ollama":
        return OllamaProvider(config.LLM_BASE_URL or config.OLLAMA_HOST)
    if choice == "openai_compat":
        return OpenAICompatProvider(config.LLM_BASE_URL or "http://127.0.0.1:8080")
    if choice == "llamacpp":
        return llamacpp.ensure_running()
    if choice == "foundation":
        return _foundation
    return None


def _running_servers() -> list[Provider]:
    found = []
    for candidate in AUTO_CANDIDATES:
        provider = _build(*candidate)
        if provider.available():
            found.append(provider)
    return found


def _pick_embeddings(servers: list[Provider]) -> tuple[Provider | None, str | None]:
    for provider in servers:
        models = provider.models()
        if config.EMBED_MODEL in models or (
            config.EMBED_MODEL.split(":")[0] in {m.split(":")[0] for m in models}
        ):
            match = next((m for m in models if m.split(":")[0] == config.EMBED_MODEL.split(":")[0]), config.EMBED_MODEL)
            return provider, match
    for provider in servers:
        embedders = [m for m in provider.models() if is_embedding_model(m)]
        if embedders:
            return provider, embedders[0]
    # llama-server started with --embeddings serves embeddings from its one model.
    for provider in servers:
        if provider.id.startswith("llamacpp"):
            models = provider.models()
            if models:
                return provider, models[0]
    return None, None


def _resolve_now() -> Resolution:
    configured = _configured_provider()
    servers = _running_servers()
    if configured and configured.kind != "foundation" and configured.available():
        servers = [configured] + [s for s in servers if s.base_url != configured.base_url]
    embed_provider, embed_model = _pick_embeddings(servers)

    if config.LLM_PROVIDER == "foundation":
        if _foundation.available():
            return Resolution("ok", "", _foundation, _foundation.largest_chat_model(), embed_provider, embed_model)
        configured = None

    primary = configured if configured and configured.kind != "foundation" and configured.available() else None
    if primary is None and config.LLM_PROVIDER in ("auto", ""):
        primary = next((s for s in servers if s.chat_models()), None)

    if primary is not None:
        models = primary.chat_models()
        wanted = config.LIVE_MODEL
        if wanted in models:
            return Resolution("ok", "", primary, wanted, embed_provider, embed_model)
        # Tolerate an implied ":latest" tag either way.
        loose = next((m for m in models if m.split(":")[0] == wanted.split(":")[0]), None)
        if loose:
            return Resolution("ok", "", primary, loose, embed_provider, embed_model)
        substitute = primary.largest_chat_model()
        if substitute:
            message = (f"{wanted} is not installed; using {substitute}"
                       if primary.kind == "ollama" else f"using {substitute} from {primary.label}")
            status = "degraded" if primary.kind == "ollama" else "ok"
            return Resolution(status, message, primary, substitute, embed_provider, embed_model)

    if config.LLM_PROVIDER not in ("auto", "", "foundation") and configured is None:
        unavailable = f"{config.LLM_PROVIDER} is not running"
    else:
        unavailable = "No local model server is running"

    if _foundation.available():
        return Resolution(
            "degraded", f"{unavailable}; using Apple Intelligence (answers less reliable)",
            _foundation, _foundation.largest_chat_model(), embed_provider, embed_model,
        )
    return Resolution("none", f"{unavailable}. Install Ollama or choose a GGUF file in Settings.",
                      fixes=FIXES)


_lock = threading.Lock()
_cached: Resolution | None = None
CACHE_SECONDS = 15.0


def resolve(refresh: bool = False) -> Resolution:
    """Current resolution, re-probed at most every CACHE_SECONDS."""
    global _cached
    with _lock:
        if refresh or _cached is None or time.monotonic() - _cached.resolved_at > CACHE_SECONDS:
            previous = _cached.public() if _cached else None
            _cached = _resolve_now()
            if previous != _cached.public():
                log.info("provider: %s %s/%s %s", _cached.status,
                         _cached.provider.id if _cached.provider else "-", _cached.model, _cached.message)
        return _cached


def status() -> dict:
    return resolve().public()


def detect() -> dict:
    """Everything the Settings and onboarding model pages show."""
    providers = []
    for candidate in AUTO_CANDIDATES:
        provider = _build(*candidate)
        running = provider.available()
        models = provider.models() if running else []
        providers.append({
            "id": provider.id, "kind": provider.kind, "label": provider.label, "url": provider.base_url,
            "running": running, "models": models,
            "chat_models": [m for m in models if not is_embedding_model(m)],
        })
    providers.append({
        "id": "foundation", "kind": "foundation", "label": _foundation.label, "url": "on-device",
        "running": _foundation.available(), "models": [], "chat_models": [],
    })
    binary = llamacpp.find_server_binary()
    return {
        "providers": providers,
        "active": resolve(refresh=True).public(),
        "llamacpp_binary": bool(binary),
    }


# ---------------------------------------------------------------- call sites

def _require() -> Resolution:
    resolution = resolve()
    if resolution.provider is None or not resolution.model:
        raise ProviderError(resolution.message or "no model provider available")
    return resolution


def chat_stream(system: str, prompt: str, *, max_tokens: int, temperature: float = 0.1,
                num_ctx: int | None = None) -> Iterator[str]:
    resolution = _require()
    yield from resolution.provider.chat_stream(
        resolution.model, system, prompt, num_ctx=num_ctx or config.LIVE_NUM_CTX,
        num_predict=max_tokens, temperature=temperature)


def chat(system: str, prompt: str, *, max_tokens: int, temperature: float = 0.1,
         num_ctx: int | None = None, timeout: float = 90.0, model: str | None = None) -> str:
    resolution = _require()
    return resolution.provider.chat(
        model or resolution.model, system, prompt, num_ctx=num_ctx or config.LIVE_NUM_CTX,
        num_predict=max_tokens, temperature=temperature, timeout=timeout)


def foundation() -> FoundationProvider:
    return _foundation


def embed(texts: list[str]) -> list[list[float]]:
    resolution = resolve()
    if resolution.embed_provider is None or not resolution.embed_model:
        raise ProviderError("no embedding model available; retrieval is disabled")
    return resolution.embed_provider.embed(resolution.embed_model, texts)


def warm() -> dict:
    resolution = resolve(refresh=True)
    if resolution.provider is None:
        return {"chat": "no provider"}
    result = resolution.provider.warm(resolution.model, None, config.LIVE_NUM_CTX)
    if resolution.embed_provider is not None and resolution.embed_model:
        try:
            resolution.embed_provider.embed(resolution.embed_model, ["warm"])
            result["embed"] = "warm"
        except ProviderError as error:
            result["embed"] = f"failed: {error}"
    log.info("model warm-up: %s", result)
    return result


def test_answer() -> dict:
    """One tiny completion, for the walkthrough's "Test an answer" button."""
    resolution = resolve(refresh=True)
    if resolution.provider is None:
        return {"error": resolution.message}
    started = time.monotonic()
    try:
        text = resolution.provider.chat(
            resolution.model, "Answer in one short sentence.", "Say that the model is working.",
            num_ctx=2048, num_predict=24, temperature=0.0, timeout=120)
    except ProviderError as error:
        # The message can quote the model server's response, which may include
        # paths or a traceback. It stays in the log.
        log.info("provider test failed: %s", error)
        return {"error": "unavailable"}
    return {"ok": True, "ms": round((time.monotonic() - started) * 1000), "text": text[:200],
            "provider": resolution.provider.label, "model": resolution.model,
            "status": resolution.status}
