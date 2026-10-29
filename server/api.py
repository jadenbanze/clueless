"""Local HTTP/WebSocket API for the overlay.

Process tree, which matters for permissions:

    Electron  (holds Microphone + AudioCapture grants)
      └── python api.py
            ├── audiotap --source mic      (spawned child -> inherits Electron's mic grant)
            └── audiotap --source system   (open -n -> own bundle identity -> AudioCapture)

macOS attributes a TCC request to the topmost owning application, so the mic
helper inherits Electron's grant through the intermediate Python process. The
system-audio helper must NOT be a child, because AudioCapture is never granted
to a child process — it needs its own bundle identity via LaunchServices.

Run: python3 -m server.api
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import brain, config, ollama_client, rag
from .socket_server import TranscriptSocketServer, TranscriptStore, Turn

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("api")

SOCKET_PATH = os.path.join(tempfile.gettempdir(), "clueless.sock")


class Hub:
    """Fan-out to connected overlays."""

    def __init__(self) -> None:
        self.clients: list[WebSocket] = []

    async def broadcast(self, payload: dict) -> None:
        for client in list(self.clients):
            try:
                await client.send_json(payload)
            except Exception:
                if client in self.clients:
                    self.clients.remove(client)


hub = Hub()
store = TranscriptStore(max_turns=config.TRANSCRIPT_MAX_TURNS)
retrieval: rag.Store | None = None
session = {
    "system": None, "mic": None, "started_at": None, "socket": None,
    # Runtime-switchable retrieval sources; see config.DEFAULT_SOURCES.
    "sources": set(k for k, v in config.DEFAULT_SOURCES.items() if v),
    "armed": False, "capturing": False, "with_mic": True, "paused": False,
}

# The event loop is captured once at startup. Calling asyncio.get_event_loop()
# from a worker thread raises RuntimeError there (there is no loop in that
# thread), and swallowing that made every broadcast silently vanish — the
# transcript arrived over HTTP but never over the WebSocket.
_loop: asyncio.AbstractEventLoop | None = None

# Speculative answers: when the transcript contains a question, an answer is
# generated before the hotkey is pressed. Keyed on the question text alone rather
# than a transcript hash — the transcript grows every few seconds, so a hash key
# would never hit. If the conversation moves on, the inferred question changes
# and the cache simply misses, which is the correct behaviour.
_speculative: dict = {"question": None, "answer": None, "ts": 0.0, "turns_at": 0}
_spec_lock = threading.Lock()
_spec_inflight: str | None = None
SPECULATIVE_TTL_SECONDS = 120


def _publish(payload: dict) -> None:
    """Sends to connected overlays from any thread."""
    if _loop is None or not _loop.is_running():
        log.debug("dropping event, loop not ready: %s", payload.get("type"))
        return
    asyncio.run_coroutine_threadsafe(hub.broadcast(payload), _loop)


def _on_activity(speaker: str, speaking: bool) -> None:
    _publish({"type": "activity", "speaker": speaker, "speaking": speaking})


def _on_turn(turn: Turn) -> None:
    _publish({"type": "turn", **turn.as_dict()})

    # Speculating the instant a question is asked answers it before anyone has
    # replied — measured, that produced "No info in the loaded context" for a
    # question the very next turn answered. So keep re-speculating while the
    # question is still recent, and let the cache follow the conversation.
    recent = [t.text for t in store.snapshot()[-4:]]
    question = _infer_question(recent)
    if _looks_like_question(question):
        _speculate(question)


def _speculate(question: str) -> None:
    """Generates an answer in the background for a question just spoken.

    This is the whole point of the feature: a hotkey press should feel instant,
    and the only way to do that with a 1.8s model is to have the answer ready
    before the key is pressed.
    """
    global _spec_inflight

    key = _normalise(question)
    with _spec_lock:
        cached = _speculative.get("question")
        # One generation at a time, and never re-run the same question.
        if _spec_inflight is not None or cached == key:
            return
        _spec_inflight = key

    recent = store.recent_text(90)

    def run() -> None:
        global _spec_inflight
        try:
            answer = brain.answer_question_streaming(
                question, recent, retrieval,
                sources=set(session["sources"]),
                screen_text=session.get("screen_text"),
                intent="answer",
            )
            # An abstention is almost always "the answer has not been said yet",
            # which is exactly what speculation gets wrong. Never cache it: a
            # fast wrong answer is worse than a slow right one.
            if brain.ABSTENTION.lower() in answer.text.lower():
                log.info("speculation abstained; not caching (%r)", question[:50])
                return

            with _spec_lock:
                _speculative.update({
                    "question": key, "answer": answer, "ts": time.time(),
                    "turns_at": len(store.snapshot()),
                })
            log.info("speculative answer ready in %dms for %r", answer.elapsed_ms, question[:60])
        except Exception:
            log.exception("speculative generation failed")
        finally:
            with _spec_lock:
                _spec_inflight = None

    threading.Thread(target=run, daemon=True, name="speculate").start()


def start_system_helper() -> bool | None:
    """Launches the system-audio helper through LaunchServices.

    Must be `open -n` (a fresh instance under its own bundle identity). Spawned
    as a child it would inherit this process's grants, and AudioCapture is never
    granted to a child — the tap would open and return silence.
    """
    log_path = os.path.join(tempfile.gettempdir(), "mc_system_helper.log")
    result = subprocess.run(
        [
            "open", "-n", "-a", str(config.TAP_APP), "--args",
            "--transcribe", "--source", "system",
            "--bundle", config.TARGET_APP_MATCH,
            "--socket", SOCKET_PATH, "--log", log_path,
            "--locale", config.SPEECH_LOCALE,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("could not launch system helper: %s", result.stderr.strip())
        return None
    log.info("system-audio helper launched (log: %s)", log_path)
    # LaunchServices gives no process handle, so record a marker. Returning None
    # made /health report system:false while the helper was demonstrably running
    # and had already sent its ready message.
    return True


def detect_call() -> bool:
    """True when the target app is currently producing audio."""
    try:
        result = subprocess.run(
            [str(config.TAP_BINARY), "--detect"],
            capture_output=True, text=True, timeout=15,
        )
        return "active=1" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _start_capture(with_mic: bool) -> None:
    session["system"] = start_system_helper()
    if with_mic:
        session["mic"] = start_mic_helper()
    session["capturing"] = True
    config.OLLAMA_KEEP_ALIVE = config.OLLAMA_KEEP_ALIVE_IN_MEETING
    threading.Thread(target=ollama_client.warm, daemon=True, name="warmup").start()
    log.info("capture started")


def _stop_capture() -> None:
    subprocess.run(["pkill", "-f", "CluelessTap"], capture_output=True)
    session["system"] = None
    session["mic"] = None
    session["capturing"] = False
    config.OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "90s")
    log.info("capture stopped")


def call_watcher() -> None:
    """Starts capture when a call begins and stops it when the call ends.

    Without this the app records from launch regardless, holding an open audio
    tap and a resident 8B model through every empty hour of the day.
    """
    last_active = 0.0
    while True:
        time.sleep(config.CALL_POLL_SECONDS)
        if session.get("paused"):
            continue
        active = detect_call()
        if active:
            last_active = time.time()
            if not session.get("capturing") and session.get("armed"):
                _start_capture(session.get("with_mic", True))
        elif session.get("capturing"):
            if time.time() - last_active > config.CALL_END_GRACE_SECONDS:
                _stop_capture()


def start_mic_helper() -> subprocess.Popen | None:
    """Spawns the mic helper as a child so it inherits the parent's mic grant."""
    # Diagnostics are written to a file rather than discarded. Discarding them
    # made a hallucination-on-noise problem invisible: the transcript was full of
    # invented sentences and there was no way to see the gate statistics that
    # would have explained why.
    log_path = os.path.join(tempfile.gettempdir(), "mc_mic_helper.log")
    try:
        log_file = open(log_path, "wb")
    except OSError:
        log_file = subprocess.DEVNULL

    process = subprocess.Popen(
        [
            str(config.TAP_BINARY), "--transcribe", "--source", "mic",
            "--socket", SOCKET_PATH, "--locale", config.SPEECH_LOCALE,
        ],
        stdout=subprocess.DEVNULL, stderr=log_file,
    )
    log.info("microphone helper spawned (pid %d, log: %s)", process.pid, log_path)
    return process


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global retrieval, _loop

    # Capture the running loop so worker threads can publish to it.
    _loop = asyncio.get_running_loop()

    try:
        retrieval = rag.Store()
        log.info(
            "retrieval ready: %d jira chunks, %d code chunks",
            retrieval.count(config.JIRA_COLLECTION),
            retrieval.count(config.CODE_COLLECTION),
        )
    except Exception as error:
        log.warning("retrieval unavailable: %s", error)
        retrieval = None

    server = TranscriptSocketServer(
        SOCKET_PATH, store, on_turn=_on_turn, on_activity=_on_activity
    )
    server.start()

    if config.AUTO_DETECT_CALLS:
        threading.Thread(target=call_watcher, daemon=True, name="call-watcher").start()
        log.info("auto call detection on (poll %ds, grace %ds)",
                 config.CALL_POLL_SECONDS, config.CALL_END_GRACE_SECONDS)
    session["socket"] = server
    yield

    server.stop()
    for key in ("system", "mic"):
        process = session.get(key)
        if isinstance(process, subprocess.Popen) and process.poll() is None:
            process.terminate()
    subprocess.run(["pkill", "-f", "CluelessTap"], capture_output=True)


app = FastAPI(title="Clueless", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str | None = None
    window_seconds: float = 120.0
    # answer (default) | suggest | explain
    intent: str = "answer"


# No body model for /summary: the client posts with no payload, and declaring a
# Pydantic body parameter makes FastAPI reject that with 422.


@app.get("/health")
def health() -> dict:
    import os

    return {
        "ok": True,
        "version": config.VERSION,
        "ollama": __import__("server.ollama_client", fromlist=["is_available"]).is_available(),
        "turns": len(store.snapshot()),
        "jira_chunks": retrieval.count(config.JIRA_COLLECTION) if retrieval else 0,
        "confluence_chunks": retrieval.count(config.CONFLUENCE_COLLECTION) if retrieval else 0,
        "code_chunks": retrieval.count(config.CODE_COLLECTION) if retrieval else 0,
        "helpers": {k: bool(session.get(k)) for k in ("system", "mic")},
        "capturing": session.get("capturing", False),
        "paused": session.get("paused", False),
        "armed": session.get("armed", False),
        "auto_detect": config.AUTO_DETECT_CALLS,
        # Surfaced so it is obvious whether .env was picked up. Only presence is
        # reported, never the values.
        "developer_mode": config.DEVELOPER_MODE,
        "jira_credentials": {
            "email_set": bool(os.environ.get("ATLASSIAN_EMAIL")),
            "token_set": bool(os.environ.get("ATLASSIAN_API_TOKEN")),
        },
    }


@app.post("/meeting/start")
def meeting_start(with_mic: bool = True) -> dict:
    session["started_at"] = time.time()
    session["armed"] = True
    session["with_mic"] = with_mic

    if config.AUTO_DETECT_CALLS:
        # Arm and let the watcher start capture when a call is actually detected.
        log.info("armed; waiting for a call to begin")
        return {"started": True, "mic": with_mic, "auto_detect": True}

    _start_capture(with_mic)
    return {"started": True, "mic": with_mic, "auto_detect": False}


@app.post("/meeting/stop")
def meeting_stop() -> dict:
    session["armed"] = False
    session["started_at"] = None
    _stop_capture()
    return {"stopped": True, "turns": len(store.snapshot())}


class ScreenRequest(BaseModel):
    image: str  # data URL: "data:image/png;base64,..."


@app.post("/screen")
def ingest_screen(request: ScreenRequest) -> dict:
    """OCR a screenshot so its text can be used as meeting context.

    Capture happens in Electron (desktopCapturer) because that is where the
    Screen Recording permission lives; extraction happens here via the Swift
    Vision helper, which runs entirely on-device.
    """
    import base64
    import tempfile

    payload = request.image
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="image is not valid base64")

    if not config.OCR_BINARY.exists():
        raise HTTPException(
            status_code=503, detail=f"OCR helper not built at {config.OCR_BINARY}"
        )

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(raw)
        path = handle.name

    try:
        result = subprocess.run(
            [str(config.OCR_BINARY), path], capture_output=True, text=True, timeout=30
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    text = result.stdout.strip()
    if not text:
        return {"ok": False, "chars": 0, "reason": "no text recognised"}

    session["screen_text"] = text
    log.info("screen context: %d chars captured", len(text))
    return {"ok": True, "chars": len(text), "preview": text[:160]}


SETTING_KEYS = (
    "JIRA_SITE", "JIRA_PROJECT_KEY", "JIRA_BOARD_ID", "JIRA_CLOUD_ID",
    "GITHUB_ORG", "GITHUB_ACCOUNT", "GITHUB_REPOS", "CONFLUENCE_SPACES",
    "SPEECH_LOCALE", "CLUELESS_CORRECTIONS", "DEVELOPER_MODE",
)


@app.get("/settings")
def get_settings() -> dict:
    """Current values for the settings window. Never includes credentials."""
    import os as _os

    def value(key: str) -> str:
        return str(getattr(config, key, "") if key in ("GITHUB_REPOS", "CONFLUENCE_SPACES")
                   else _os.environ.get(key, getattr(config, key, "")))

    return {
        **{key: value(key) for key in SETTING_KEYS},
        "GITHUB_REPOS": ",".join(config.GITHUB_REPOS),
        "CONFLUENCE_SPACES": ",".join(config.CONFLUENCE_SPACES),
        "version": config.VERSION,
    }


@app.post("/settings")
def save_settings(payload: dict) -> dict:
    """Persists non-secret settings and reports which need a restart.

    Deliberately does not write .env: that file holds API tokens and a UI save
    should never rewrite it.
    """
    import json as _json

    config.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    try:
        current = _json.loads(config.SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        pass

    for key in SETTING_KEYS:
        if key in payload:
            current[key] = str(payload[key]).strip()
    config.SETTINGS_PATH.write_text(_json.dumps(current, indent=1))
    log.info("settings saved: %s", sorted(payload))
    return {"saved": True, "restart_required": True, "settings": current}


@app.post("/pause")
def pause() -> dict:
    """Stops listening without quitting the app.

    Distinct from /meeting/stop: pausing keeps the app running and the tray
    active, so it can be resumed instantly — the common case is stepping away
    from a sensitive conversation, not ending the session.
    """
    session["paused"] = True
    _stop_capture()
    log.info("paused")
    _publish({"type": "paused", "paused": True})
    return {"paused": True}


@app.post("/resume")
def resume() -> dict:
    session["paused"] = False
    session["armed"] = True
    if not config.AUTO_DETECT_CALLS:
        _start_capture(session.get("with_mic", True))
    log.info("resumed")
    _publish({"type": "paused", "paused": False})
    return {"paused": False, "auto_detect": config.AUTO_DETECT_CALLS}


@app.get("/sources")
def get_sources() -> dict:
    enabled = session["sources"]
    # Only report sources this mode actually offers, so the UI never shows a
    # control that would be rejected.
    available = [n for n in ("jira", "confluence", "code", "screen")
                 if config.DEVELOPER_MODE or n not in config.DEVELOPER_SOURCES]
    return {
        **{name: name in enabled for name in available},
        "developer_mode": config.DEVELOPER_MODE,
    }


@app.post("/sources/{name}/toggle")
def toggle_source(name: str) -> dict:
    """Flips one retrieval source on or off and returns the new state."""
    if name not in ("jira", "confluence", "code", "screen"):
        raise HTTPException(status_code=400, detail=f"unknown source '{name}'")
    if name in config.DEVELOPER_SOURCES and not config.DEVELOPER_MODE:
        raise HTTPException(
            status_code=403,
            detail=f"'{name}' requires developer mode (set DEVELOPER_MODE=1 in .env)",
        )
    enabled: set[str] = session["sources"]
    if name in enabled:
        enabled.discard(name)
    else:
        enabled.add(name)
    log.info("sources now: %s", sorted(enabled))
    state = {
        **{n: n in enabled for n in ("jira", "confluence", "code", "screen")},
        "developer_mode": config.DEVELOPER_MODE,
    }
    _publish({"type": "sources", **state})
    return state


@app.get("/transcript")
def transcript(seconds: float | None = None) -> dict:
    if seconds:
        return {"text": store.recent_text(seconds)}
    return {"turns": [t.as_dict() for t in store.snapshot()]}


_QUESTION_STARTERS = (
    "who", "what", "when", "where", "why", "how", "which", "whose",
    "can", "could", "should", "would", "will", "do", "does", "did",
    "is", "are", "was", "were", "have", "has", "any", "anything",
)


def _looks_like_question(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped.split()) < 3:
        return False
    if "?" in stripped:
        return True
    return stripped.split()[0].strip(",.").lower() in _QUESTION_STARTERS


def _normalise(question: str) -> str:
    return " ".join((question or "").lower().split())


def _infer_question(recent: list[str]) -> str:
    """Turns the recent transcript into something worth answering.

    Pressing the hotkey does not always follow a question — often someone is
    narrating ("we are sitting on the release branch"). Using the last utterance
    verbatim made the overlay echo the speaker back at itself, which is useless.
    When there is no question, ask for a briefing instead.
    """
    if not recent:
        return "What is being discussed?"

    for text in reversed(recent):
        stripped = text.strip()
        if not stripped:
            continue
        first_word = stripped.split()[0].strip(",.").lower()
        if "?" in stripped or first_word in _QUESTION_STARTERS:
            return stripped

    # No question was asked. Ask for orientation rather than parroting.
    return (
        "Nobody asked a question. Briefly: what is being discussed right now, "
        "and is there anything I should know or contribute?"
    )


def _broadcast(payload: dict) -> None:
    _publish(payload)


@app.post("/ask")
def ask(request: AskRequest) -> dict:
    recent = store.recent_text(request.window_seconds)
    if not recent.strip():
        return {"text": "No meeting audio captured yet.", "citations": [], "elapsed_ms": 0}

    # With no explicit question, infer one from the transcript — this is the
    # hotkey case, where the user reacts to what was just said.
    question = request.question
    if not question:
        turns = store.snapshot()
        if not turns:
            question = "What is being discussed?"
        else:
            question = _infer_question([t.text for t in turns[-3:]])

    # Serve a pre-generated answer if it is still about the same question.
    if not request.question and request.intent == "answer":
        with _spec_lock:
            spec = dict(_speculative)
        age = time.time() - spec.get("ts", 0)
        moved = len(store.snapshot()) - spec.get("turns_at", 0)
        if (
            spec.get("answer") is not None
            and spec.get("question") == _normalise(question)
            and age < SPECULATIVE_TTL_SECONDS
            # More than two new turns means the conversation has moved on and
            # the pre-generated answer may no longer reflect it.
            and 0 <= moved <= 2
        ):
            payload = spec["answer"].as_dict()
            payload.update({"question": question, "cached": True, "age_ms": int(age * 1000)})
            _broadcast({"type": "answer", **payload})
            log.info("served speculative answer (age %dms)", int(age * 1000))
            return {"streaming": False, "cached": True, "question": question}

    # Generation runs on a worker thread so the HTTP request returns at once and
    # the overlay can paint tokens as they arrive. Waiting for the full answer
    # left it blank for several seconds, which reads as broken.
    def generate() -> None:
        _broadcast({"type": "answer_start", "question": question})
        try:
            answer = brain.answer_question_streaming(
                question, recent, retrieval,
                on_delta=lambda piece: _broadcast({"type": "answer_delta", "text": piece}),
                sources=set(session["sources"]),
                screen_text=session.get("screen_text"),
                intent=request.intent,
            )
        except Exception as error:  # surface failures in the overlay, not just logs
            log.exception("answer generation failed")
            _broadcast({"type": "answer", "text": f"Answer failed: {error}",
                        "citations": [], "elapsed_ms": 0, "question": question})
            return

        payload = answer.as_dict()
        payload["question"] = question
        _broadcast({"type": "answer", **payload})

    threading.Thread(target=generate, daemon=True, name="answer").start()
    return {"streaming": True, "question": question}


@app.post("/summary")
def summary() -> dict:
    text = store.full_text()
    if not text.strip():
        return {"text": "No transcript to summarise.", "citations": [], "elapsed_ms": 0}
    return brain.summarise(text).as_dict()


@app.websocket("/events")
async def events(websocket: WebSocket) -> None:
    await websocket.accept()
    hub.clients.append(websocket)
    try:
        # Replay so a late-joining overlay is not blank.
        for turn in store.snapshot():
            await websocket.send_json({"type": "turn", **turn.as_dict()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in hub.clients:
            hub.clients.remove(websocket)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
