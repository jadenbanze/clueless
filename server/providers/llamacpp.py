"""Managed llama.cpp: Clueless starts `llama-server` for a local GGUF file.

For machines that cannot reach a model registry: copy a .gguf file over, point
Settings at it, and nothing else needs to be installed or running. The server
binds to loopback only and is stopped when the API exits.
"""

from __future__ import annotations

import atexit
import logging
import shutil
# Fixed argv, no shell.
import subprocess  # nosec B404
import time
from pathlib import Path

from .. import config
from . import http
from .openai_compat import OpenAICompatProvider

log = logging.getLogger(__name__)

_process: subprocess.Popen | None = None


def find_server_binary() -> str | None:
    if config.LLAMACPP_SERVER and Path(config.LLAMACPP_SERVER).is_file():
        return config.LLAMACPP_SERVER
    for candidate in ("/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server"):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("llama-server")


def _stop() -> None:
    global _process
    if _process and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None


atexit.register(_stop)


def ensure_running(timeout: float = 90.0) -> OpenAICompatProvider | None:
    """Starts llama-server for GGUF_PATH if needed; returns a provider once it answers."""
    global _process
    url = f"http://127.0.0.1:{config.LLAMACPP_MANAGED_PORT}"
    provider = OpenAICompatProvider(url, provider_id="llamacpp", label="llama.cpp (managed)")
    if provider.available():
        return provider
    gguf = Path(config.GGUF_PATH) if config.GGUF_PATH else None
    binary = find_server_binary()
    if not gguf or not gguf.is_file() or gguf.suffix.lower() != ".gguf" or not binary:
        log.warning("managed llama.cpp unavailable: gguf=%s binary=%s", bool(gguf and gguf.is_file()), bool(binary))
        return None
    if _process is None or _process.poll() is not None:
        log.info("starting llama-server for %s", gguf.name)
        log_path = config.LOG_DIR / "llama-server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab")  # noqa: SIM115 — owned by the child for its lifetime
        _process = subprocess.Popen(  # noqa: S603  # nosec B603
            [binary, "-m", str(gguf), "--host", "127.0.0.1", "--port", str(config.LLAMACPP_MANAGED_PORT),
             "--ctx-size", str(max(config.LIVE_NUM_CTX, 4096)), "--embeddings"],
            stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process.poll() is not None:
            log.error("llama-server exited with %s; see llama-server.log", _process.returncode)
            return None
        if http.reachable(f"{url}/v1/models", timeout=0.5):
            return provider
        time.sleep(0.5)
    log.error("llama-server did not answer within %.0fs", timeout)
    return None
