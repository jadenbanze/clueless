"""Configuration for Clueless.

Values are module attributes backed by ``reload()``, so saving in the Settings
window updates live state in memory without dropping an active meeting's
transcript.
"""

from __future__ import annotations

import json as _json
import os
import re
from pathlib import Path

from . import profiles as _profiles

INSTALL_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = INSTALL_ROOT


def _resolve_data_dir() -> Path:
    raw = os.environ.get("CLUELESS_DATA_DIR", "").strip()
    return Path(raw).expanduser() if raw else INSTALL_ROOT


DATA_DIR = _resolve_data_dir()


def _load_env_files() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(DATA_DIR / ".env", override=True)
        if DATA_DIR != INSTALL_ROOT and not (DATA_DIR / ".env").exists():
            load_dotenv(INSTALL_ROOT / ".env", override=False)
    except ImportError:
        pass


_load_env_files()

VERSION = os.environ.get("CLUELESS_VERSION", "dev")

LOG_DIR = DATA_DIR / "logs"
API_LOG = LOG_DIR / "api.log"
MIC_HELPER_LOG = LOG_DIR / "mic-helper.log"
SYSTEM_HELPER_LOG = LOG_DIR / "system-helper.log"
SETTINGS_PATH = DATA_DIR / ".cache" / "settings.json"
AUTO_VOCAB_PATH = DATA_DIR / ".cache" / "auto-vocabulary.json"

_saved: dict = {}


def _read_saved() -> dict:
    try:
        return _json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _setting(key: str, default: str = "") -> str:
    saved = _saved.get(key)
    if saved not in (None, ""):
        return str(saved)
    return os.environ.get(key) or default


def _setting_list(key: str, sep_pattern: str = r",") -> list[str]:
    return [item.strip() for item in re.split(sep_pattern, _setting(key)) if item.strip()]


# --- Native helpers ------------------------------------------------------
TAP_APP = INSTALL_ROOT / "native" / "build" / "CluelessTap.app"
TAP_BINARY = TAP_APP / "Contents" / "MacOS" / "audiotap"
OCR_BINARY = INSTALL_ROOT / "native" / "build" / "ocr"
FM_CLI = INSTALL_ROOT / "native" / "build" / "fmcli"
PDF_BINARY = INSTALL_ROOT / "native" / "build" / "pdf"
CALENDAR_BINARY = INSTALL_ROOT / "native" / "build" / "calendar"

LLM_BACKEND = os.environ.get("LLM_BACKEND", "")
LIVE_BACKEND = os.environ.get("LIVE_BACKEND", LLM_BACKEND or "ollama")
SUMMARY_BACKEND = os.environ.get("SUMMARY_BACKEND", LLM_BACKEND or "foundation")

TARGET_APPS = [
    "microsoft.teams2",
    "cisco-systems.spark",
    "com.apple.facetime",
    "avconferenced",
    "zoom.us",
    "skype",
]
TARGET_APP_MATCH = ",".join(TARGET_APPS)

TRANSCRIPT_MAX_TURNS = 400

# Generic employer-neutral speech corrections; user & auto-harvested pairs layer on top.
BASE_SPEECH_CORRECTIONS: dict[str, str] = {
    "red only": "read only",
    "red-only": "read-only",
    "redonly": "read-only",
    "kafka": "Kafka",
    "coffee authentication": "Kafka authentication",
    "coffee authentication failure": "Kafka authentication failure",
    "audit right path": "audit write path",
    "right path remediation": "write path remediation",
    "jwt": "JWT",
    "oauth": "OAuth",
    "o auth": "OAuth",
    "graphql": "GraphQL",
    "graph ql": "GraphQL",
    "postgres": "Postgres",
    "postgress": "Postgres",
    "post gre SQL": "PostgreSQL",
    "postgre sql": "PostgreSQL",
    "kube ctl": "kubectl",
    "kube control": "kubectl",
    "terra form": "Terraform",
    "github actions": "GitHub Actions",
    "git hub": "GitHub",
    "bit bucket": "Bitbucket",
    "ci cd": "CI/CD",
    "rest api": "REST API",
    "json web token": "JWT",
    "open api": "OpenAPI",
    "swagger ui": "Swagger UI",
}

SPEECH_CORRECTIONS: dict[str, str] = dict(BASE_SPEECH_CORRECTIONS)

OLLAMA_HOST = "http://127.0.0.1:11434"
LLAMACPP_MANAGED_PORT = 8081
ALLOW_REMOTE_PROVIDER = os.environ.get("ALLOW_REMOTE_PROVIDER", "").lower() in ("1", "true", "yes")
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "90s")
OLLAMA_KEEP_ALIVE_IN_MEETING = os.environ.get("OLLAMA_KEEP_ALIVE_IN_MEETING", "30m")

SUMMARY_NUM_CTX = 16384
SUMMARY_NUM_PREDICT = 900

# --- Collections & retrieval budgets -------------------------------------
CHROMA_DIR = DATA_DIR / ".chroma"
JIRA_COLLECTION = "jira_issues"
CODE_COLLECTION = "repo_code"
CONFLUENCE_COLLECTION = "confluence_pages"
NOTES_COLLECTION = "local_notes"
ANKI_COLLECTION = "anki_cards"
PUBMED_COLLECTION = "pubmed_papers"
CANVAS_COLLECTION = "canvas_courses"
NOTION_COLLECTION = "notion_pages"

SOURCE_COLLECTIONS: dict[str, str] = {
    "jira": JIRA_COLLECTION,
    "confluence": CONFLUENCE_COLLECTION,
    "code": CODE_COLLECTION,
    "notes": NOTES_COLLECTION,
    "anki": ANKI_COLLECTION,
    "pubmed": PUBMED_COLLECTION,
    "canvas": CANVAS_COLLECTION,
    "notion": NOTION_COLLECTION,
}

RETRIEVE_TOP_K = 4
DEVELOPER_SOURCES = ("jira", "confluence", "code")
STUDENT_SOURCES = ("anki", "pubmed", "canvas")
ALL_SOURCES = tuple(_profiles.SOURCE_CATALOG.keys())

JIRA_MAX_ISSUES = 800
CONFLUENCE_MAX_PAGES = 400

CODE_INCLUDE_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".tf", ".tfvars",
    ".py", ".sql", ".yaml", ".yml", ".json", ".md", ".sh", ".gradle",
}
CODE_EXCLUDE_DIRS = {
    "node_modules", ".git", "dist", "build", "target", "vendor",
    ".terraform", "__pycache__", "coverage", ".next", "out",
}
CODE_MAX_FILE_BYTES = 180_000
CODE_CHUNK_LINES = 120
CODE_CHUNK_OVERLAP_LINES = 20

CODE_LOW_VALUE_MARKERS = (
    "text.json",
    "messages/",
    "/i18n/",
    "/locales/",
    "package-lock.json",
    ".min.",
    "schema.graphql",
)

NOTES_INCLUDE_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".csv", ".pdf", ".pptx", ".docx"}

CONTEXT_MAX_CHARS_PER_CHUNK = 800
CONTEXT_MAX_TOTAL_CHARS = 2000
TRANSCRIPT_MAX_CHARS = 1500

CALL_POLL_SECONDS = 15
CALL_END_GRACE_SECONDS = 180
HOST = "127.0.0.1"
PORT = 8766

_TRUE = ("1", "true", "yes")

# Dynamically populated by reload() below.
SPEECH_LOCALE = "en-US"
LLM_PROVIDER = "auto"
LLM_BASE_URL = ""
LLAMACPP_SERVER = ""
GGUF_PATH = ""
LIVE_MODEL = "llama3.1:8b"
SUMMARY_MODEL = "llama3.1:8b"
EMBED_MODEL = "nomic-embed-text:latest"
LIVE_NUM_CTX = 4096
LIVE_NUM_PREDICT = 90
LIVE_TEMPERATURE = 0.1

PROFILE = "general"
CLINICAL_MODE = False
PROFILE_SETTINGS: dict = {}
DEVELOPER_MODE = False
STUDENT_MODE = False
NETWORK_INDEXING = True
REDACT_PHI = False
DEFAULT_SOURCES: dict[str, bool] = {}
AVAILABLE_SOURCES: tuple[str, ...] = ()
HOTKEY_SOURCES: tuple[str, ...] = ()

USER_NAME = ""
USER_ROLE = ""
USER_FOCUS = ""

LOCAL_NOTES_DIRS: list[str] = []
ANKI_PATH = ""
ANKI_DECK_FILTER = ""
PUBMED_QUERY = ""
ZOTERO_BIB_PATH = ""
CANVAS_URL = ""
NOTION_QUERY = ""
CALENDAR_ENABLED = True

JIRA_CLOUD_ID = ""
JIRA_SITE = ""
JIRA_PROJECT_KEY = ""
JIRA_BOARD_ID = ""
CONFLUENCE_SPACES: list[str] = []
GITHUB_ORG = ""
GITHUB_ACCOUNT = ""
GITHUB_REPOS: list[str] = []
AUTO_DETECT_CALLS = True


def reload() -> None:
    """Re-reads .env and settings.json so changes apply without restarting."""
    global DATA_DIR, LOG_DIR, API_LOG, MIC_HELPER_LOG, SYSTEM_HELPER_LOG
    global SETTINGS_PATH, AUTO_VOCAB_PATH, CHROMA_DIR, _saved
    global SPEECH_LOCALE, SPEECH_CORRECTIONS
    global LLM_PROVIDER, LLM_BASE_URL, LLAMACPP_SERVER, GGUF_PATH
    global LIVE_MODEL, SUMMARY_MODEL, EMBED_MODEL, LIVE_NUM_CTX, LIVE_NUM_PREDICT, LIVE_TEMPERATURE
    global PROFILE, CLINICAL_MODE, PROFILE_SETTINGS, DEVELOPER_MODE, STUDENT_MODE
    global NETWORK_INDEXING, REDACT_PHI, DEFAULT_SOURCES, AVAILABLE_SOURCES, HOTKEY_SOURCES
    global USER_NAME, USER_ROLE, USER_FOCUS
    global LOCAL_NOTES_DIRS, ANKI_PATH, ANKI_DECK_FILTER, PUBMED_QUERY, ZOTERO_BIB_PATH
    global CANVAS_URL, NOTION_QUERY, CALENDAR_ENABLED
    global JIRA_CLOUD_ID, JIRA_SITE, JIRA_PROJECT_KEY, JIRA_BOARD_ID
    global CONFLUENCE_SPACES, GITHUB_ORG, GITHUB_ACCOUNT, GITHUB_REPOS, AUTO_DETECT_CALLS

    DATA_DIR = _resolve_data_dir()
    LOG_DIR = DATA_DIR / "logs"
    API_LOG = LOG_DIR / "api.log"
    MIC_HELPER_LOG = LOG_DIR / "mic-helper.log"
    SYSTEM_HELPER_LOG = LOG_DIR / "system-helper.log"
    SETTINGS_PATH = DATA_DIR / ".cache" / "settings.json"
    AUTO_VOCAB_PATH = DATA_DIR / ".cache" / "auto-vocabulary.json"
    CHROMA_DIR = DATA_DIR / ".chroma"

    _load_env_files()
    _saved = _read_saved()

    SPEECH_LOCALE = _setting("SPEECH_LOCALE", "en-US")

    merged_corrections = dict(BASE_SPEECH_CORRECTIONS)
    # Auto-harvested pairs sit below explicit user pairs so manual edits always win.
    try:
        auto_vocab = _json.loads(AUTO_VOCAB_PATH.read_text())
        for wrong, right in (auto_vocab.get("pairs") or {}).items():
            if wrong and right:
                merged_corrections[str(wrong).strip().lower()] = str(right).strip()
    except (OSError, ValueError):
        pass

    extra = _setting("CLUELESS_CORRECTIONS", "")
    for pair in extra.split(","):
        if "=" in pair:
            wrong, right = pair.split("=", 1)
            if wrong.strip() and right.strip():
                merged_corrections[wrong.strip().lower()] = right.strip()
    SPEECH_CORRECTIONS.clear()
    SPEECH_CORRECTIONS.update(merged_corrections)

    LLM_PROVIDER = _setting("LLM_PROVIDER", "auto")
    LLM_BASE_URL = _setting("LLM_BASE_URL", "")
    LLAMACPP_SERVER = _setting("LLAMACPP_SERVER", "")
    GGUF_PATH = _setting("GGUF_PATH", "")
    LIVE_MODEL = _setting("LIVE_MODEL", "llama3.1:8b")
    SUMMARY_MODEL = _setting("SUMMARY_MODEL", "llama3.1:8b")
    EMBED_MODEL = _setting("EMBED_MODEL", "nomic-embed-text:latest")
    LIVE_NUM_CTX = int(_setting("LIVE_NUM_CTX", "4096"))
    LIVE_NUM_PREDICT = int(_setting("LIVE_NUM_PREDICT", "90"))
    LIVE_TEMPERATURE = float(_setting("LIVE_TEMPERATURE", "0.1"))

    dev_flag = _setting("DEVELOPER_MODE", "").lower() in _TRUE
    PROFILE = _profiles.resolve(_setting("PROFILE", ""), dev_flag)
    clinical_flag = _setting("CLINICAL_MODE", "").lower() in _TRUE
    CLINICAL_MODE = PROFILE == "medical" or (PROFILE == "student" and clinical_flag)
    PROFILE_SETTINGS = _profiles.get(PROFILE, clinical=CLINICAL_MODE)

    DEVELOPER_MODE = bool(PROFILE_SETTINGS["developer_mode"] or (dev_flag and not CLINICAL_MODE))
    STUDENT_MODE = bool(PROFILE_SETTINGS.get("student_mode"))
    NETWORK_INDEXING = bool(PROFILE_SETTINGS["network_indexing"])

    redact = _setting("REDACT_PHI", "")
    REDACT_PHI = True if CLINICAL_MODE else ((redact.lower() in _TRUE) if redact else bool(PROFILE_SETTINGS["redact_phi"]))

    AVAILABLE_SOURCES = tuple(PROFILE_SETTINGS.get("sources", ("screen",)))
    if DEVELOPER_MODE and not CLINICAL_MODE:
        for src in DEVELOPER_SOURCES:
            if src not in AVAILABLE_SOURCES:
                AVAILABLE_SOURCES = (*AVAILABLE_SOURCES, src)
    HOTKEY_SOURCES = tuple(PROFILE_SETTINGS.get("hotkey_sources", ("screen",)))

    DEFAULT_SOURCES = {src: False for src in ALL_SOURCES}
    for src, enabled in (PROFILE_SETTINGS.get("default_sources") or {}).items():
        DEFAULT_SOURCES[src] = bool(enabled)
    if DEVELOPER_MODE and not CLINICAL_MODE:
        DEFAULT_SOURCES["jira"] = True
        DEFAULT_SOURCES["confluence"] = True

    USER_NAME = _setting("USER_NAME", "")
    USER_ROLE = _setting("USER_ROLE", "")
    USER_FOCUS = _setting("USER_FOCUS", "")

    LOCAL_NOTES_DIRS = _setting_list("LOCAL_NOTES_DIRS", r"[;\n,]")
    ANKI_PATH = _setting("ANKI_PATH", "")
    ANKI_DECK_FILTER = _setting("ANKI_DECK_FILTER", "")
    PUBMED_QUERY = _setting("PUBMED_QUERY", "")
    ZOTERO_BIB_PATH = _setting("ZOTERO_BIB_PATH", "")
    CANVAS_URL = _setting("CANVAS_URL", "")
    NOTION_QUERY = _setting("NOTION_QUERY", "")
    CALENDAR_ENABLED = _setting("CALENDAR_ENABLED", "1").lower() in _TRUE
    if not CALENDAR_ENABLED:
        DEFAULT_SOURCES["calendar"] = False

    JIRA_CLOUD_ID = _setting("JIRA_CLOUD_ID")
    JIRA_SITE = _setting("JIRA_SITE")
    JIRA_PROJECT_KEY = _setting("JIRA_PROJECT_KEY")
    JIRA_BOARD_ID = _setting("JIRA_BOARD_ID")
    CONFLUENCE_SPACES = _setting_list("CONFLUENCE_SPACES")
    GITHUB_ORG = _setting("GITHUB_ORG")
    GITHUB_ACCOUNT = _setting("GITHUB_ACCOUNT")
    GITHUB_REPOS = _setting_list("GITHUB_REPOS")

    AUTO_DETECT_CALLS = _setting("AUTO_DETECT_CALLS", "1").lower() in _TRUE


reload()
