"""Local HTTP/WebSocket API for the overlay.

Process tree, which matters for permissions:

    Electron  (holds Microphone + AudioCapture + Calendar grants)
      └── python api.py
            ├── audiotap --source mic      (spawned child -> inherits Electron's mic grant)
            └── audiotap --source system   (open -n -> own bundle identity -> AudioCapture)

Run: python3 -m server.api
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import (
    brain,
    config,
    context_sources,
    harvest_corrections,
    indexers,
    phi,
    profiles,
    providers,
    rag,
    security,
    socket_server,
)
from .settings_schema import SECRET_KEYS, SETTING_KEYS, CredentialsUpdate, SettingsUpdate
from .socket_server import TranscriptSocketServer, TranscriptStore, Turn


def _configure_logging() -> None:
    from logging.handlers import RotatingFileHandler

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.LOG_DIR, 0o700)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(config.API_LOG, maxBytes=1_000_000, backupCount=5))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        handlers=handlers,
    )


_configure_logging()
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
session: dict = {
    "system": None,
    "mic": None,
    "started_at": None,
    "socket": None,
    "sources": {k for k, v in config.DEFAULT_SOURCES.items() if v},
    "armed": False,
    "capturing": False,
    "with_mic": True,
    "paused": False,
    "screen_text": None,
    "screen_ts": 0.0,
}

_index_state: dict = {
    "running": False,
    "step": "",
    "error": None,
    "last_result": None,
}
_index_lock = threading.Lock()
_helper_restart_ts: dict[str, float] = {"me": 0.0, "them": 0.0}

_loop: asyncio.AbstractEventLoop | None = None

_speculative: dict = {"question": None, "answer": None, "ts": 0.0, "turns_at": 0}
_answer_cache: dict[tuple[str, str, int], brain.Answer] = {}
_spec_lock = threading.Lock()
_spec_inflight: str | None = None
_user_ask_active = threading.Event()
SPECULATIVE_TTL_SECONDS = 120

_REDACTED_FIELDS = ("text", "question", "full")


def _redacted(payload: dict) -> dict:
    """Every outbound event passes here so PHI redaction cannot be bypassed."""
    if not config.REDACT_PHI:
        return payload
    out = dict(payload)
    for field_name in _REDACTED_FIELDS:
        if isinstance(out.get(field_name), str):
            out[field_name] = phi.redact(out[field_name])
    if isinstance(out.get("citations"), list):
        out["citations"] = [phi.redact(str(c)) for c in out["citations"]]
    if isinstance(out.get("citation_links"), list):
        # Never forward external URLs when PHI redaction is active.
        out["citation_links"] = [
            {"label": phi.redact(str(item.get("label", "")))}
            for item in out["citation_links"]
            if isinstance(item, dict)
        ]
    return out


def _publish(payload: dict) -> None:
    payload = _redacted(payload)
    if _loop is None or not _loop.is_running():
        log.debug("dropping event, loop not ready: %s", payload.get("type"))
        return
    asyncio.run_coroutine_threadsafe(hub.broadcast(payload), _loop)


def _publish_status() -> None:
    _publish({
        "type": "status",
        "capturing": bool(session.get("capturing")),
        "armed": bool(session.get("armed")),
        "paused": bool(session.get("paused")),
    })


def _on_activity(speaker: str, speaking: bool) -> None:
    _publish({"type": "activity", "speaker": speaker, "speaking": speaking})


def _on_helper_disconnect(source: str) -> None:
    """Supervises helper sockets: auto-restarts a crashed stream during an active call."""
    if not session.get("capturing") or session.get("paused"):
        return
    now = time.monotonic()
    if now - _helper_restart_ts.get(source, 0.0) < 12.0:
        log.warning("helper [%s] disconnected repeatedly; backing off", source)
        return
    _helper_restart_ts[source] = now

    def _respawn() -> None:
        time.sleep(1.5)
        if not session.get("capturing") or session.get("paused"):
            return
        sock = session.get("socket")
        if sock and source in sock.connected_sources:
            return
        log.warning("helper [%s] disconnected mid-session; restarting", source)
        if source == "me" and session.get("with_mic", True):
            session["mic"] = start_mic_helper()
        elif source == "them":
            session["system"] = start_system_helper()

    threading.Thread(target=_respawn, daemon=True, name=f"respawn-{source}").start()


def _active_calendar_text() -> str | None:
    if "calendar" not in session["sources"] or config.CLINICAL_MODE:
        return None
    cal_text, attendees = context_sources.get_calendar_context()
    if attendees:
        socket_server.rebuild_corrections(attendees)
    return cal_text or None


def _on_turn(turn: Turn) -> None:
    _publish({"type": "turn", **turn.as_dict()})

    # Skip background speculation while a user-triggered /ask is actively running
    # so we never queue behind ourselves on single-slot local model servers.
    if _user_ask_active.is_set():
        return
    recent = [t.text for t in store.snapshot()[-4:]]
    question = _infer_question(recent)
    if _looks_like_question(question):
        _speculate(question)


def _speculate(question: str) -> None:
    global _spec_inflight

    key = _normalise(question)
    with _spec_lock:
        cached = _speculative.get("question")
        if _spec_inflight is not None or cached == key:
            return
        _spec_inflight = key

    recent = store.recent_text(90)

    def run() -> None:
        global _spec_inflight
        try:
            if _user_ask_active.is_set():
                return
            answer = brain.answer_question_streaming(
                question,
                recent,
                retrieval,
                sources=set(session["sources"]),
                screen_text=session.get("screen_text") if "screen" in session["sources"] else None,
                calendar_text=_active_calendar_text(),
                intent="answer",
            )
            if brain.ABSTENTION.lower() in answer.text.lower():
                log.info("speculation abstained; not caching (%d-char question)", len(question))
                return

            with _spec_lock:
                _speculative.update({
                    "question": key,
                    "answer": answer,
                    "ts": time.time(),
                    "turns_at": len(store.snapshot()),
                })
            log.info("speculative answer ready in %dms (%d-char question)", answer.elapsed_ms, len(question))
        except Exception:
            log.exception("speculative generation failed")
        finally:
            with _spec_lock:
                _spec_inflight = None

    threading.Thread(target=run, daemon=True, name="speculate").start()


def start_system_helper() -> bool | None:
    log_path = str(config.SYSTEM_HELPER_LOG)
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
    return True


def detect_call() -> bool:
    try:
        result = subprocess.run(
            [str(config.TAP_BINARY), "--detect"],
            capture_output=True, text=True, timeout=15,
        )
        return "active=1" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _start_capture(with_mic: bool) -> None:
    session["capturing"] = True
    session["system"] = start_system_helper()
    if with_mic:
        session["mic"] = start_mic_helper()
    config.OLLAMA_KEEP_ALIVE = config.OLLAMA_KEEP_ALIVE_IN_MEETING
    threading.Thread(target=providers.warm, daemon=True, name="warmup").start()
    _publish_status()
    log.info("capture started")


def _stop_capture() -> None:
    session["capturing"] = False
    mic_proc = session.get("mic")
    if isinstance(mic_proc, subprocess.Popen) and mic_proc.poll() is None:
        mic_proc.terminate()
    subprocess.run(["pkill", "-9", "-x", "audiotap"], capture_output=True)
    session["system"] = None
    session["mic"] = None
    config.OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "90s")
    _publish_status()
    log.info("capture stopped")


def call_watcher() -> None:
    last_active = 0.0
    while True:
        time.sleep(config.CALL_POLL_SECONDS)
        if session.get("paused") or not config.AUTO_DETECT_CALLS:
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
    log_path = str(config.MIC_HELPER_LOG)
    try:
        log_file = open(log_path, "wb")  # noqa: SIM115
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

    _loop = asyncio.get_running_loop()

    try:
        retrieval = rag.Store()
        counts = retrieval.counts()
        log.info("retrieval ready: %s", counts)
    except Exception as error:
        log.warning("retrieval unavailable: %s", error)
        retrieval = None

    server = TranscriptSocketServer(
        SOCKET_PATH,
        store,
        on_turn=_on_turn,
        on_activity=_on_activity,
        on_disconnect=_on_helper_disconnect,
    )
    server.start()

    threading.Thread(target=call_watcher, daemon=True, name="call-watcher").start()
    session["socket"] = server
    yield

    server.stop()
    _stop_capture()


app = FastAPI(title="Clueless", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
API_TOKEN = security.load_or_create_token()
app.add_middleware(security.LoopbackGuard, token=API_TOKEN, port=config.PORT)


@app.exception_handler(Exception)
async def _unhandled(request: Request, error: Exception) -> JSONResponse:
    request_id = secrets.token_hex(4)
    log.exception("unhandled error %s on %s %s", request_id, request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal error", "request_id": request_id})


@app.exception_handler(RequestValidationError)
async def _invalid(_request: Request, error: RequestValidationError) -> JSONResponse:
    problems = [
        {"field": ".".join(str(p) for p in item.get("loc", ())[1:]), "message": item.get("msg", "")}
        for item in error.errors()
    ]
    first = problems[0] if problems else {"field": "", "message": "invalid request"}
    return JSONResponse(
        status_code=422,
        content={"error": f"{first['field']}: {first['message']}".strip(": "), "problems": problems},
    )


@app.get("/livez")
def livez() -> dict:
    return {"ok": True}


class AskRequest(BaseModel):
    question: str | None = Field(default=None, max_length=500)
    previous_question: str | None = Field(default=None, max_length=300)
    previous_answer: str | None = Field(default=None, max_length=600)
    window_seconds: float = Field(default=120.0, ge=5, le=3600)
    intent: str = Field(default="answer", pattern=r"^(answer|suggest|explain)$")


def _credential_presence() -> dict[str, bool]:
    return {key: bool(os.environ.get(key, "").strip()) for key in SECRET_KEYS}


@app.get("/health")
def health() -> dict:
    counts = retrieval.counts() if retrieval else {}
    sock: TranscriptSocketServer | None = session.get("socket")
    connected = sock.connected_sources if sock else set()
    return {
        "ok": True,
        "version": config.VERSION,
        "profile": config.PROFILE,
        "clinical_mode": config.CLINICAL_MODE,
        "provider": providers.status(),
        "turns": len(store.snapshot()),
        "jira_chunks": counts.get(config.JIRA_COLLECTION, 0),
        "confluence_chunks": counts.get(config.CONFLUENCE_COLLECTION, 0),
        "code_chunks": counts.get(config.CODE_COLLECTION, 0),
        "chunks": {src: counts.get(col, 0) for src, col in config.SOURCE_COLLECTIONS.items()},
        "helpers": {
            "system": ("them" in connected) if session.get("capturing") else bool(session.get("system")),
            "mic": ("me" in connected) if session.get("capturing") else bool(session.get("mic")),
        },
        "capturing": session.get("capturing", False),
        "paused": session.get("paused", False),
        "armed": session.get("armed", False),
        "auto_detect": config.AUTO_DETECT_CALLS,
        "developer_mode": config.DEVELOPER_MODE,
        "jira_credentials": {
            "email_set": bool(os.environ.get("ATLASSIAN_EMAIL")),
            "token_set": bool(os.environ.get("ATLASSIAN_API_TOKEN")),
        },
        "credentials": _credential_presence(),
    }


@app.post("/meeting/start")
def meeting_start(with_mic: bool = True, force: bool = False) -> dict:
    session["started_at"] = time.time()
    session["armed"] = True
    session["with_mic"] = with_mic

    if config.AUTO_DETECT_CALLS and not force:
        log.info("armed; waiting for a call to begin")
        _publish_status()
        return {"started": True, "capturing": bool(session.get("capturing")), "mic": with_mic, "auto_detect": True}

    if not session.get("capturing"):
        _start_capture(with_mic)
    return {"started": True, "capturing": True, "mic": with_mic, "auto_detect": False}


@app.post("/meeting/stop")
def meeting_stop() -> dict:
    session["armed"] = False
    session["started_at"] = None
    _stop_capture()
    return {"stopped": True, "turns": len(store.snapshot())}


MAX_SCREEN_B64_CHARS = 12_000_000


class ScreenRequest(BaseModel):
    image: str = Field(max_length=MAX_SCREEN_B64_CHARS)


@app.post("/screen")
def ingest_screen(request: ScreenRequest) -> dict:
    import base64

    payload = request.image
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="image is not valid base64") from None
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=400, detail="image must be a PNG")

    if not config.OCR_BINARY.exists():
        log.error("OCR helper missing at %s", config.OCR_BINARY)
        raise HTTPException(status_code=503, detail="OCR helper is not installed")

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
    session["screen_ts"] = time.time()
    log.info("screen context: %d chars captured", len(text))
    return _redacted({"ok": True, "chars": len(text), "text": text[:160]})


@app.get("/settings")
def get_settings() -> dict:
    """Current values for the settings window. Never includes credentials."""

    def value(key: str) -> str:
        attr = getattr(config, key, "")
        if isinstance(attr, (list, tuple)):
            sep = ";" if key == "LOCAL_NOTES_DIRS" else ","
            attr = sep.join(attr)
        elif isinstance(attr, bool):
            attr = "1" if attr else "0"
        return config._setting(key, str(attr if attr is not None else ""))

    return {
        **{key: value(key) for key in SETTING_KEYS},
        "version": config.VERSION,
        "data_dir": str(config.DATA_DIR),
        "credentials": _credential_presence(),
    }


def _sources_payload() -> dict:
    enabled: set[str] = session["sources"]
    available = list(config.AVAILABLE_SOURCES)
    return {
        **{name: (name in enabled) for name in available},
        "available": available,
        "hotkey_sources": list(config.HOTKEY_SOURCES),
        "labels": {k: v["short"] for k, v in profiles.SOURCE_CATALOG.items()},
        "developer_mode": config.DEVELOPER_MODE,
        "student_mode": config.STUDENT_MODE,
        "clinical_mode": config.CLINICAL_MODE,
    }


@app.post("/settings")
def save_settings(update: SettingsUpdate) -> dict:
    """Persists non-secret settings and hot-reloads configuration in memory."""
    prev_profile = (config.PROFILE, config.CLINICAL_MODE)
    config.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    try:
        current = _json.loads(config.SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        pass

    changes = update.model_dump(exclude_none=True)
    current.update(changes)
    config.SETTINGS_PATH.write_text(_json.dumps(current, indent=1))

    config.reload()
    socket_server.rebuild_corrections()
    providers.resolve(refresh=True)

    # Reconcile active sources if the persona or clinical mode changed.
    if (config.PROFILE, config.CLINICAL_MODE) != prev_profile:
        session["sources"] = {k for k, v in config.DEFAULT_SOURCES.items() if v and k in config.AVAILABLE_SOURCES}
    else:
        session["sources"] = {s for s in session["sources"] if s in config.AVAILABLE_SOURCES}

    _publish({"type": "profile", **get_profile()})
    _publish({"type": "sources", **_sources_payload()})
    log.info("settings saved & hot-reloaded: %s", sorted(changes))
    return {"saved": True, "restart_required": False, "settings": current}


def update_env_credentials(changes: dict[str, str]) -> dict[str, bool]:
    """Safely updates write-only credentials in DATA_DIR/.env with mode 0600."""
    env_path = config.DATA_DIR / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if env_path.exists():
        try:
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing_lines = []

    updated_keys: set[str] = set()
    out_lines: list[str] = []
    for line in existing_lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in changes:
            key = match.group(1)
            updated_keys.add(key)
            val = changes[key]
            if val:
                out_lines.append(f"{key}={val}")
            else:
                os.environ.pop(key, None)
        else:
            out_lines.append(line)

    for key, val in changes.items():
        if key not in updated_keys:
            if val:
                out_lines.append(f"{key}={val}")
            else:
                os.environ.pop(key, None)

    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out_lines).rstrip() + "\n")

    for key, val in changes.items():
        if val:
            os.environ[key] = val
    config.reload()
    return _credential_presence()


@app.get("/credentials/status")
def credentials_status() -> dict:
    return {"credentials": _credential_presence()}


@app.post("/credentials")
def save_credentials(update: CredentialsUpdate) -> dict:
    """Write-only endpoint for API tokens; values are stored in .env (0600) and never returned."""
    changes = update.model_dump(exclude_none=True)
    presence = update_env_credentials(changes)
    log.info("credentials updated (keys: %s)", sorted(changes))
    return {"saved": True, "credentials": presence}


class IndexRequest(BaseModel):
    sources: list[str] | None = None
    refresh: bool = False


@app.get("/index/status")
def index_status() -> dict:
    counts = retrieval.counts() if retrieval else {}
    manifest = indexers.load_manifest()
    return {
        "running": _index_state["running"],
        "step": _index_state["step"],
        "error": _index_state["error"],
        "last_synced_at": manifest.get("last_synced_at"),
        "last_result": _index_state["last_result"],
        "network_indexing": config.NETWORK_INDEXING,
        "chunks": {src: counts.get(col, 0) for src, col in config.SOURCE_COLLECTIONS.items()},
        "calendar": context_sources.calendar_status(request_access=False),
    }


@app.post("/index")
def trigger_index(request: IndexRequest | None = None) -> dict:
    req = request or IndexRequest()
    if req.sources:
        unknown = [s for s in req.sources if s not in config.SOURCE_COLLECTIONS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown source(s): {', '.join(unknown)}")

    with _index_lock:
        if _index_state["running"]:
            return {"started": False, "running": True, "step": _index_state["step"]}
        _index_state.update({"running": True, "step": "Starting sync…", "error": None})

    def _run() -> None:
        def _progress(msg: str) -> None:
            _index_state["step"] = msg
            _publish({"type": "index_status", "running": True, "step": msg})

        try:
            target_store = retrieval or rag.Store()
            res = indexers.build_all(
                refresh=req.refresh,
                incremental=not req.refresh,
                sources=req.sources,
                on_progress=_progress,
                store=target_store,
            )
            _index_state.update({"running": False, "step": "Complete", "last_result": res, "error": None})
            _publish({"type": "index_status", "running": False, "step": "Complete", "result": res})
        except Exception as error:
            log.exception("background indexing failed")
            _index_state.update({"running": False, "step": "", "error": str(error)[:240]})
            _publish({"type": "index_status", "running": False, "error": str(error)[:240]})

    threading.Thread(target=_run, daemon=True, name="indexer").start()
    return {"started": True, "running": True}


@app.post("/speech/harvest")
def speech_harvest() -> dict:
    _cal_text, attendees = context_sources.get_calendar_context()
    res = harvest_corrections.harvest_to_cache(extra_names=attendees)
    return {"ok": True, **res}


@app.post("/calendar/permission")
def request_calendar_permission() -> dict:
    status = context_sources.calendar_status(request_access=True)
    if status.get("authorized"):
        context_sources.get_calendar_context(force=True)
    return status


@app.get("/logs/{name}")
def get_log(name: str, lines: int = 300) -> dict:
    paths = {"api": config.API_LOG, "mic": config.MIC_HELPER_LOG, "system": config.SYSTEM_HELPER_LOG}
    if name not in paths:
        raise HTTPException(status_code=404, detail="unknown log")
    lines = max(10, min(lines, 2000))
    try:
        with open(paths[name], "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return {"name": name, "text": ""}
    return {"name": name, "text": "\n".join(text.splitlines()[-lines:])}


@app.post("/pause")
def pause() -> dict:
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
    else:
        _publish_status()
    log.info("resumed")
    _publish({"type": "paused", "paused": False})
    return {"paused": False, "auto_detect": config.AUTO_DETECT_CALLS}


@app.get("/sources")
def get_sources() -> dict:
    return _sources_payload()


@app.post("/sources/{name}/toggle")
def toggle_source(name: str) -> dict:
    if name not in config.ALL_SOURCES:
        raise HTTPException(status_code=400, detail=f"unknown source '{name}'")
    if name not in config.AVAILABLE_SOURCES:
        raise HTTPException(
            status_code=403,
            detail=f"'{name}' is not available in the {config.PROFILE} profile",
        )
    if config.CLINICAL_MODE and profiles.SOURCE_CATALOG.get(name, {}).get("network"):
        raise HTTPException(status_code=403, detail="network sources are disabled in Clinical mode")

    enabled: set[str] = session["sources"]
    if name in enabled:
        enabled.discard(name)
    else:
        enabled.add(name)
    log.info("sources now: %s", sorted(enabled))
    state = _sources_payload()
    _publish({"type": "sources", **state})
    return state


@app.get("/transcript")
def transcript(seconds: float | None = None) -> dict:
    if seconds:
        return _redacted({"text": store.recent_text(seconds)})
    return {"turns": [_redacted(t.as_dict()) for t in store.snapshot()]}


@app.get("/providers")
def get_providers() -> dict:
    return providers.detect()


@app.post("/providers/test")
def test_provider() -> dict:
    result = providers.test_answer()
    if not result.get("ok"):
        return JSONResponse(
            status_code=503,
            content={"error": "The model did not answer. Check that a local model is running."},
        )
    return {
        "ok": True,
        "ms": result.get("ms"),
        "provider": result.get("provider"),
        "model": result.get("model"),
    }


@app.get("/profile")
def get_profile() -> dict:
    return {
        **profiles.public(config.PROFILE, clinical=config.CLINICAL_MODE),
        "redact_phi": config.REDACT_PHI,
    }


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
    if not recent:
        return "What is being discussed?"

    for text in reversed(recent):
        stripped = text.strip()
        if not stripped:
            continue
        first_word = stripped.split()[0].strip(",.").lower()
        if "?" in stripped or first_word in _QUESTION_STARTERS:
            return stripped

    return (
        "Nobody asked a question. Briefly: what is being discussed right now, "
        "and is there anything I should know or contribute?"
    )


def _broadcast(payload: dict) -> None:
    _publish(payload)


def _stream_delta_handler(streamed: list[str]):
    def on_delta(piece: str) -> None:
        if not config.REDACT_PHI:
            _broadcast({"type": "answer_delta", "text": piece})
            return
        streamed.append(piece)
        so_far = "".join(streamed)
        cut = so_far.rfind(" ")
        if cut > 0:
            _broadcast({"type": "answer_delta", "text": "", "full": so_far[:cut]})

    return on_delta


@app.post("/ask")
def ask(request: AskRequest) -> dict:
    recent = store.recent_text(request.window_seconds)
    # `explain` works over screen OCR even when no speech has been captured yet.
    has_screen = bool(session.get("screen_text")) and (request.intent == "explain" or "screen" in session["sources"])
    if not recent.strip() and not has_screen:
        return {"text": "No meeting audio captured yet.", "citations": [], "elapsed_ms": 0}

    question = request.question
    if not question:
        turns = store.snapshot()
        question = _infer_question([t.text for t in turns[-3:]]) if turns else (
            "What am I looking at, and why does it matter here?" if request.intent == "explain" else "What is being discussed?"
        )

    # Thread optional follow-up context concisely into the transcript tail.
    if request.previous_question and request.previous_answer:
        recent = (
            f"{recent}\n[Earlier Q&A — Q: {request.previous_question[:160]} | "
            f"A: {request.previous_answer[:280]}]"
        ).strip()

    turn_count = len(store.snapshot())
    cache_key = (request.intent, _normalise(question), turn_count)

    if not request.previous_question and cache_key in _answer_cache:
        cached_ans = _answer_cache[cache_key]
        payload = cached_ans.as_dict()
        payload.update({"question": question, "cached": True})
        _broadcast({"type": "answer", **payload})
        return {"streaming": False, "cached": True, "question": question}

    if not request.question and request.intent == "answer":
        with _spec_lock:
            spec = dict(_speculative)
        age = time.time() - spec.get("ts", 0)
        moved = turn_count - spec.get("turns_at", 0)
        if (
            spec.get("answer") is not None
            and spec.get("question") == _normalise(question)
            and age < SPECULATIVE_TTL_SECONDS
            and 0 <= moved <= 2
        ):
            payload = spec["answer"].as_dict()
            payload.update({"question": question, "cached": True, "age_ms": int(age * 1000)})
            _broadcast({"type": "answer", **payload})
            log.info("served speculative answer (age %dms)", int(age * 1000))
            return {"streaming": False, "cached": True, "question": question}

    streamed: list[str] = []
    on_delta = _stream_delta_handler(streamed)
    screen_ctx = session.get("screen_text") if has_screen else None

    def generate() -> None:
        _user_ask_active.set()
        _broadcast({"type": "answer_start", "question": question})
        try:
            answer = brain.answer_question_streaming(
                question,
                recent,
                retrieval,
                on_delta=on_delta,
                sources=set(session["sources"]),
                screen_text=screen_ctx,
                calendar_text=_active_calendar_text(),
                intent=request.intent,
            )
        except Exception:
            request_id = secrets.token_hex(4)
            log.exception("answer generation failed (%s)", request_id)
            _broadcast({
                "type": "answer",
                "text": f"Answer failed. Check that your model provider is running (ref {request_id}).",
                "citations": [],
                "citation_links": [],
                "elapsed_ms": 0,
                "question": question,
            })
            return
        finally:
            _user_ask_active.clear()

        if brain.ABSTENTION.lower() not in answer.text.lower() and len(_answer_cache) < 64:
            _answer_cache[cache_key] = answer
        payload = answer.as_dict()
        payload["question"] = question
        _broadcast({"type": "answer", **payload})

    threading.Thread(target=generate, daemon=True, name="answer").start()
    return {"streaming": True, "question": question}


@app.post("/summary")
def summary(stream: bool = False) -> dict:
    text = store.full_text()
    if not text.strip():
        return {"text": "No transcript to summarise.", "citations": [], "elapsed_ms": 0}

    if not stream:
        return _redacted(brain.summarise(text).as_dict())

    streamed: list[str] = []
    on_delta = _stream_delta_handler(streamed)

    def generate_summary() -> None:
        _broadcast({"type": "answer_start", "question": "Recap of the session so far"})
        try:
            ans = brain.summarise_streaming(text, on_delta=on_delta)
        except Exception:
            request_id = secrets.token_hex(4)
            log.exception("summary generation failed (%s)", request_id)
            _broadcast({
                "type": "answer",
                "text": f"Summary failed. Check that your model provider is running (ref {request_id}).",
                "citations": [],
                "elapsed_ms": 0,
                "format": "summary",
            })
            return
        payload = ans.as_dict()
        payload.update({"question": "Recap of the session so far", "format": "summary"})
        _broadcast({"type": "answer", **payload})

    threading.Thread(target=generate_summary, daemon=True, name="summary").start()
    return {"streaming": True}


@app.websocket("/events")
async def events(websocket: WebSocket) -> None:
    await websocket.accept(subprotocol=security.websocket_subprotocol(websocket.scope))
    hub.clients.append(websocket)
    try:
        await websocket.send_json({
            "type": "status",
            "capturing": bool(session.get("capturing")),
            "armed": bool(session.get("armed")),
            "paused": bool(session.get("paused")),
        })
        for turn in store.snapshot():
            await websocket.send_json(_redacted({"type": "turn", **turn.as_dict()}))
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
