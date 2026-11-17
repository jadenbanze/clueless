"""Unix-socket listener for transcript turns from the Swift helpers.

Two helper processes feed this socket because macOS attributes audio permissions
differently across launch paths:

    system audio -> started via LaunchServices, uses its own bundle's grant
    microphone   -> spawned as a child of Electron, inherits Electron's grant

Also handles:
  * live-reloadable deterministic vocabulary repairs + conservative phonetic
    proper-noun matching from indexed context / attendee names
  * cross-stream acoustic bleed suppression (laptop speakers bleeding into mic)
  * connection lifecycle callbacks so crashed helpers can be supervised
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
from difflib import SequenceMatcher
from typing import Callable

from . import config
from .textclean import strip_fillers

log = logging.getLogger(__name__)

_COMMON_CAPITALIZED = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december", "today", "tomorrow", "yesterday", "morning",
    "afternoon", "evening", "everyone", "somebody", "anyone", "nobody", "nothing",
    "something", "everything", "because", "however", "therefore", "although",
    "actually", "basically", "probably", "definitely", "currently", "question",
    "decision", "release", "service", "project", "meeting", "summary", "docker",
    "python", "apple", "macos", "windows", "linux", "google", "microsoft",
}

_compiled_snapshot: dict[str, str] | None = None
_correction_pattern: re.Pattern[str] | None = None
_domain_terms: dict[str, str] = {}  # lower -> canonical
_phonetic_buckets: dict[str, list[str]] = {}
_vocab_lock = threading.Lock()


def _phonetic_key(word: str) -> str:
    """Lightweight consonant skeleton for catching STT proper-noun near-misses."""
    low = re.sub(r"[^a-z]", "", word.lower())
    if not low:
        return ""
    low = (
        low.replace("ph", "f")
        .replace("sh", "s")
        .replace("ch", "c")
        .replace("th", "t")
        .replace("ck", "k")
        .replace("qu", "kw")
    )
    head = low[0]
    tail = re.sub(r"[aeiouyhw]", "", low[1:])
    collapsed = re.sub(r"(.)\1+", r"\1", head + tail)
    return collapsed[:6]


def rebuild_corrections(extra_terms: set[str] | list[str] | None = None) -> None:
    """Recompiles the regex and proper-noun buckets from current config."""
    global _compiled_snapshot, _correction_pattern

    with _vocab_lock:
        snapshot = dict(config.SPEECH_CORRECTIONS)
        _compiled_snapshot = snapshot
        if snapshot:
            keys = sorted(snapshot, key=len, reverse=True)
            _correction_pattern = re.compile(
                r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b",
                re.IGNORECASE,
            )
        else:
            _correction_pattern = None

        terms: set[str] = set(extra_terms or ())
        if config.USER_NAME:
            terms.update(p for p in config.USER_NAME.split() if len(p) >= 4)
        try:
            data = json.loads(config.AUTO_VOCAB_PATH.read_text())
            terms.update(data.get("terms") or ())
        except (OSError, ValueError):
            pass
        for canonical in snapshot.values():
            if " " not in canonical and "-" not in canonical and len(canonical) >= 5:
                terms.add(canonical)

        _domain_terms.clear()
        _phonetic_buckets.clear()
        for term in terms:
            clean = term.strip()
            if len(clean) < 5 or len(clean) > 32 or not clean.isalpha():
                continue
            low = clean.lower()
            if low in _COMMON_CAPITALIZED:
                continue
            _domain_terms[low] = clean
            pkey = _phonetic_key(clean)
            if len(pkey) >= 2:
                _phonetic_buckets.setdefault(pkey[:3], []).append(clean)


def _repair_proper_nouns(text: str) -> str:
    if not _phonetic_buckets or not text:
        return text

    def _replace_token(match: re.Match[str]) -> str:
        word = match.group(0)
        low = word.lower()
        # Only inspect capitalised tokens that are not already known or common words.
        if not word[0].isupper() or len(word) < 5 or low in _COMMON_CAPITALIZED or low in _domain_terms:
            return word
        pkey = _phonetic_key(word)
        if len(pkey) < 2:
            return word
        candidates = _phonetic_buckets.get(pkey[:3], ())
        best_term = None
        best_score = 0.0
        for candidate in candidates:
            if abs(len(word) - len(candidate)) > 2:
                continue
            if _phonetic_key(candidate)[:4] != pkey[:4] and len(pkey) >= 4:
                continue
            ratio = SequenceMatcher(None, low, candidate.lower()).ratio()
            if ratio >= 0.84 and ratio > best_score:
                best_score = ratio
                best_term = candidate
        return best_term if best_term else word

    return re.sub(r"\b[A-Z][a-z]{4,30}\b", _replace_token, text)


def apply_corrections(text: str) -> str:
    """Repairs known transcription errors and near-miss proper nouns."""
    if not text:
        return text
    if _compiled_snapshot != config.SPEECH_CORRECTIONS:
        rebuild_corrections()
    pattern = _correction_pattern
    if pattern:
        text = pattern.sub(
            lambda match: config.SPEECH_CORRECTIONS[match.group(0).lower()], text
        )
    return _repair_proper_nouns(text)


rebuild_corrections()


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


def _echo_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def is_acoustic_echo(
    me_text: str, me_ts: float, last_them_text: str, last_them_ts: float
) -> bool:
    """True when a microphone turn is laptop-speaker bleed of what 'them' just said."""
    if not last_them_text or me_ts - last_them_ts > 2.5 or me_ts < last_them_ts - 0.5:
        return False
    me_words = _echo_tokens(me_text)
    if len(me_words) < 3:
        return False
    them_words = _echo_tokens(last_them_text)
    if not them_words:
        return False
    overlap = len(me_words & them_words) / len(me_words)
    return overlap >= 0.75


class TranscriptSocketServer:
    def __init__(
        self,
        socket_path: str,
        store: TranscriptStore,
        on_turn: Callable[[Turn], None] | None = None,
        on_activity: Callable[[str, bool], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
    ):
        self.socket_path = socket_path
        self.store = store
        self.on_turn = on_turn
        self.on_activity = on_activity
        self.on_disconnect = on_disconnect
        self._server: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self.connected_sources: set[str] = set()
        self._last_them: tuple[str, float] = ("", 0.0)

    def start(self) -> None:
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
        peer_source: str | None = None
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
                            src = self._dispatch(line)
                            if src:
                                peer_source = src
        except OSError as error:
            log.debug("socket connection ended: %s", error)
        finally:
            if peer_source:
                self.connected_sources.discard(peer_source)
                if self._running and self.on_disconnect:
                    self.on_disconnect(peer_source)

    def _dispatch(self, raw: bytes) -> str | None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("malformed message from helper: %r", raw[:200])
            return None

        kind = message.get("type")
        if kind == "ready":
            source = str(message.get("source", "?"))
            self.connected_sources.add(source)
            log.info("helper ready: source=%s locale=%s", source, message.get("locale"))
            return source
        if kind == "turn":
            text = (message.get("text") or "").strip()
            if not text:
                return None
            cleaned = strip_fillers(text)
            if not cleaned:
                return None
            speaker = message.get("speaker", "them")
            ts = float(message.get("ts") or time.time())
            corrected = apply_corrections(cleaned)

            if speaker == "them":
                self._last_them = (corrected, ts)
            elif speaker == "me":
                last_text, last_ts = self._last_them
                if is_acoustic_echo(corrected, ts, last_text, last_ts):
                    log.info("dropped acoustic mic bleed (%d chars)", len(corrected))
                    return "me"

            turn = Turn(speaker=speaker, text=corrected, timestamp=ts)
            self.store.add(turn)
            log.info("turn [%s] %d chars", turn.speaker, len(turn.text))
            if self.on_turn:
                self.on_turn(turn)
            return speaker
        if kind == "activity":
            if self.on_activity:
                self.on_activity(
                    message.get("speaker", "them"), bool(message.get("speaking"))
                )
        elif kind == "error":
            log.error("helper error: %s", message.get("message"))
        return None
