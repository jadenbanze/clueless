"""Minimal Ollama client over HTTP.

Deliberately dependency-free: the official client adds nothing here, and this
keeps the runtime surface small.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


def _post(path: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{config.OLLAMA_HOST}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise OllamaError(f"cannot reach Ollama at {config.OLLAMA_HOST}: {error}") from error


def is_available() -> bool:
    try:
        with urllib.request.urlopen(f"{config.OLLAMA_HOST}/api/tags", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def installed_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{config.OLLAMA_HOST}/api/tags", timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [m["name"] for m in data.get("models", [])]
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []


# Last request's timings, in milliseconds. Ollama reports these on every
# response and they are the only reliable way to tell whether latency lives in
# prompt processing or token generation — a distinction that decides whether to
# shorten the prompt or cap the output.
last_timings: dict = {}


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


def chat(
    model: str,
    system: str,
    user: str,
    *,
    num_ctx: int = 8192,
    num_predict: int = 320,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> str:
    payload = {
        "model": model,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    data = _post("/api/chat", payload, timeout)
    _record_timings(data)
    message = data.get("message") or {}
    return (message.get("content") or "").strip()


# nomic-embed-text has a limited context; oversized chunks make Ollama return
# HTTP 500 rather than a useful error, so inputs are clipped before sending.
EMBED_MAX_CHARS = 6000


def chat_stream(
    model: str,
    system: str,
    user: str,
    *,
    num_ctx: int = 8192,
    num_predict: int = 320,
    temperature: float = 0.2,
    timeout: float = 120.0,
):
    """Yields response fragments as they are generated.

    Streaming matters for perceived latency: the non-streaming path leaves the
    overlay blank for 3-5 seconds, which reads as broken even though the total
    time is the same.
    """
    payload = {
        "model": model,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    request = urllib.request.Request(
        f"{config.OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
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
    except urllib.error.URLError as error:
        raise OllamaError(f"cannot reach Ollama at {config.OLLAMA_HOST}: {error}") from error


def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embeds a batch.

    Uses /api/embed (not the legacy /api/embeddings), which accepts a list and
    returns them together — one round trip per batch instead of per chunk.
    """
    if not texts:
        return []
    model = model or config.EMBED_MODEL
    clipped = [text[:EMBED_MAX_CHARS] for text in texts]
    data = _post("/api/embed", {"model": model, "input": clipped}, timeout=180)
    vectors = data.get("embeddings")
    if not vectors or len(vectors) != len(clipped):
        raise OllamaError(
            f"expected {len(clipped)} embeddings from {model}, got "
            f"{0 if not vectors else len(vectors)}"
        )
    return vectors


def warm() -> dict:
    """Loads the chat and embedding models so the first question is not cold.

    Measured: the first answer after idle spent 3,351ms in load_duration, versus
    ~3ms once warm — roughly half the total latency of that first request.
    """
    result = {}
    for label, model, path, payload in (
        ("chat", config.LIVE_MODEL, "/api/chat",
         {"messages": [{"role": "user", "content": "ok"}], "stream": False,
          "keep_alive": config.OLLAMA_KEEP_ALIVE,
          # Must match live requests: a different num_ctx triggers a reload,
          # which is exactly the cost the warm-up exists to avoid.
          "options": {"num_ctx": config.LIVE_NUM_CTX, "num_predict": 1}}),
        ("embed", config.EMBED_MODEL, "/api/embed", {"input": ["warm"]}),
    ):
        try:
            body = {"model": model, **payload}
            _post(path, body, timeout=180)
            result[label] = "warm"
        except OllamaError as error:
            result[label] = f"failed: {error}"
    log.info("model warm-up: %s", result)
    return result
