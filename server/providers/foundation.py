"""Apple Foundation Models through the `fmcli` bridge (native/fmcli.swift).

On-device, on the Neural Engine, no server and no download. Measured as less
reliable than an 8B model at multi-rule grounding (it abstained on answerable
questions), so it answers live questions only when nothing better is running.
"""

from __future__ import annotations

import json
import logging
# Runs a fixed local binary, no shell.
import subprocess  # nosec B404
import time
from collections.abc import Iterator

from .. import config
from .base import ProviderError, last_timings

log = logging.getLogger(__name__)

MODEL_NAME = "apple-on-device"


class FoundationProvider:
    kind = "foundation"
    id = "foundation"
    label = "Apple Intelligence"
    base_url = ""

    def __init__(self):
        self._available: bool | None = None

    def available(self) -> bool:
        # Availability changes only if Apple Intelligence is toggled; cache it.
        if self._available is None:
            self._available = False
            if config.FM_CLI.exists():
                try:
                    result = subprocess.run(  # noqa: S603  # nosec B603
                        [str(config.FM_CLI), "--check"], capture_output=True, text=True, timeout=10)
                    self._available = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
        return self._available

    def models(self) -> list[str]:
        return [MODEL_NAME] if self.available() else []

    chat_models = models

    def largest_chat_model(self) -> str | None:
        return MODEL_NAME if self.available() else None

    def chat_stream(self, model, system, user, *, num_ctx=0, num_predict=320, temperature=0.2,
                    timeout=120.0) -> Iterator[str]:
        started = time.monotonic()
        request = json.dumps({"instructions": system, "prompt": user, "maxTokens": num_predict})
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            [str(config.FM_CLI)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            assert process.stdin and process.stdout  # nosec B101
            process.stdin.write(request)
            process.stdin.close()
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" in event:
                    raise ProviderError(event["error"])
                if "reset" in event:
                    yield event["reset"]
                elif "delta" in event:
                    yield event["delta"]
                if event.get("done"):
                    break
        finally:
            process.wait(timeout=30)
            last_timings.clear()
            last_timings["total_ms"] = round((time.monotonic() - started) * 1000, 1)

    def chat(self, model, system, user, *, num_ctx=0, num_predict=320, temperature=0.2, timeout=90.0) -> str:
        return "".join(self.chat_stream(model, system, user, num_predict=num_predict)).strip()

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        raise ProviderError("Apple Intelligence does not provide embeddings")

    def warm(self, model: str, embed_model: str | None, num_ctx: int) -> dict:
        return {"chat": "on-device"}
