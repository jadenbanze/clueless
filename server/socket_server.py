"""Unix-socket listener for transcript turns from the Swift helpers.

Two helper processes feed this socket, and they must be separate because macOS
will not grant both audio permissions to one process identity:

    system audio -> started via LaunchServices, uses its own bundle's grant
    microphone   -> spawned as a child of Electron, inherits Electron's grant

Each sends newline-delimited JSON:

    {"type":"ready","source":"them","locale":"en_US"}
    {"type":"turn","speaker":"them","text":"...","ts":1698765432.1}
    {"type":"error","message":"..."}
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import config
from .textclean import strip_fillers

log = logging.getLogger(__name__)

# Longest keys first so "red only" wins over "red", and word boundaries stop
# corrections from firing inside unrelated words.
_CORRECTION_PATTERN = re.compile(
    r"\b(" + "|".join(
        re.escape(key) for key in sorted(config.SPEECH_CORRECTIONS, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
) if config.SPEECH_CORRECTIONS else None


def apply_corrections(text: str) -> str:
    """Repairs known transcription errors. See config.SPEECH_CORRECTIONS."""
    if not _CORRECTION_PATTERN:
        return text
    return _CORRECTION_PATTERN.sub(
        lambda match: config.SPEECH_CORRECTIONS[match.group(0).lower()], text
    )


@dataclass
class Turn:
    speaker: str  # "me" | "them"
    text: str
    timestamp: float

    def render(self) -> str:
        return f"{self.speaker}: {self.text}"

    def as_dict(self) -> dict:
        return {"speaker": self.speaker, "text": self.text, "ts": self.timestamp}


@dataclass
class TranscriptStore:
    """In-memory transcript. Never persisted — meetings are not written to disk."""

    max_turns: int = 400
    turns: list[Turn] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, turn: Turn) -> None:
        with self._lock:
            self.turns.append(turn)
            if len(self.turns) > self.max_turns:
                del self.turns[: len(self.turns) - self.max_turns]

    def snapshot(self) -> list[Turn]:
        with self._lock:
            return list(self.turns)

    def recent_text(self, seconds: float) -> str:
        cutoff = time.time() - seconds
        return "\n".join(t.render() for t in self.snapshot() if t.timestamp >= cutoff)

    def full_text(self) -> str:
        return "\n".join(t.render() for t in self.snapshot())


class TranscriptSocketServer:
    def __init__(self, socket_path: str, store: TranscriptStore,
                 on_turn: Callable[[Turn], None] | None = None,
                 on_activity: Callable[[str, bool], None] | None = None):
        self.socket_path = socket_path
        self.store = store
        self.on_turn = on_turn
        self.on_activity = on_activity
        self._server: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self.connected_sources: set[str] = set()

    def start(self) -> None:
        # A stale socket file from a crashed run would make bind() fail.
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(4)
        self._server = server
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="transcript-socket")
        self._thread.start()
        log.info("transcript socket listening at %s", self.socket_path)

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def _accept_loop(self) -> None:
        assert self._server
        while self._running:
            try:
                connection, _ = self._server.accept()
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(connection,), daemon=True, name="transcript-conn"
            ).start()

    def _handle(self, connection: socket.socket) -> None:
        buffer = b""
        try:
            with connection:
                while self._running:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            self._dispatch(line)
        except OSError as error:
            log.debug("socket connection ended: %s", error)

    def _dispatch(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("malformed message from helper: %r", raw[:200])
            return

        kind = message.get("type")
        if kind == "ready":
            source = message.get("source", "?")
            self.connected_sources.add(source)
            log.info("helper ready: source=%s locale=%s", source, message.get("locale"))
        elif kind == "turn":
            text = (message.get("text") or "").strip()
            if not text:
                return
            # Fillers first: "um, Kafka" becomes "Kafka", so the vocabulary
            # corrections afterwards see clean text.
            cleaned = strip_fillers(text)
            if not cleaned:
                # The turn was nothing but hesitation; drop it rather than
                # storing an empty row or, worse, feeding it to the model.
                return
            turn = Turn(
                speaker=message.get("speaker", "them"),
                text=apply_corrections(cleaned),
                timestamp=float(message.get("ts") or time.time()),
            )
            self.store.add(turn)
            # Speaker and length only. Logging the text would put meeting content
            # on disk (stdout is captured to a file by `nohup npm start > log`),
            # which contradicts the promise that transcripts stay in memory.
            log.info("turn [%s] %d chars", turn.speaker, len(turn.text))
            if self.on_turn:
                self.on_turn(turn)
        elif kind == "activity":
            if self.on_activity:
                self.on_activity(
                    message.get("speaker", "them"), bool(message.get("speaking"))
                )
        elif kind == "error":
            log.error("helper error: %s", message.get("message"))
