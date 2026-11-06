"""Configuration for Clueless.

Values are deliberately plain module constants: this runs as a single local
process for one user, so there is no need for layered config resolution.
"""

import os
from pathlib import Path

from . import profiles as _profiles

# Binaries and Python package live here. In a packaged build this is
# Contents/Resources (read-only under /Applications).
INSTALL_ROOT = Path(__file__).resolve().parent.parent
# Writable state (.env, settings, chroma, caches). Packaged Electron sets
# CLUELESS_DATA_DIR to ~/Library/Application Support/Clueless so the bundle
# is never written to. Unset in development → same as INSTALL_ROOT.
_data = os.environ.get("CLUELESS_DATA_DIR", "").strip()
DATA_DIR = Path(_data).expanduser() if _data else INSTALL_ROOT
# Back-compat: most call sites mean "install root" for helpers / server cwd.
PROJECT_ROOT = INSTALL_ROOT

# Load credentials from .env before anything reads os.environ.
#
# This must happen in config.py, which every other module imports: the API is
# spawned by Electron as a child process, so it does not inherit variables
# exported in a shell. A file on disk is the only way `npm start` sees them.
try:
    from dotenv import load_dotenv

    load_dotenv(DATA_DIR / ".env")
    # Dev convenience: packaged first-run may copy .env into DATA_DIR; if that
    # file is still missing, fall back to the install-tree .env when present
    # (repo checkout / unpackaged Resources during local pack testing).
    if DATA_DIR != INSTALL_ROOT and not (DATA_DIR / ".env").exists():
        load_dotenv(INSTALL_ROOT / ".env")
except ImportError:  # python-dotenv is optional; env vars still work
    pass

# Single version source is overlay/package.json; Electron passes app.getVersion().
VERSION = os.environ.get("CLUELESS_VERSION", "dev")

# Rotating logs live with the rest of the user's data, not in /tmp, so they are
# private to the user and survive a reboot for bug reports.
LOG_DIR = DATA_DIR / "logs"
API_LOG = LOG_DIR / "api.log"
MIC_HELPER_LOG = LOG_DIR / "mic-helper.log"
SYSTEM_HELPER_LOG = LOG_DIR / "system-helper.log"

# Non-secret settings written by the settings window. Kept separate from .env so
# the UI never rewrites a file containing credentials.

SETTINGS_PATH = DATA_DIR / ".cache" / "settings.json"
try:
    import json as _json

    _saved = _json.loads(SETTINGS_PATH.read_text())
except (OSError, ValueError):
    _saved = {}


def _setting(key: str, default: str = "") -> str:
    # An empty field in Settings means "use the default", not "use nothing":
    # otherwise clearing a model name would leave the app with no model.
    saved = _saved.get(key)
    if saved not in (None, ""):
        return str(saved)
    return os.environ.get(key) or default


def _setting_list(key: str) -> list[str]:
    return [item.strip() for item in _setting(key).split(",") if item.strip()]

# --- Audio capture -------------------------------------------------------
# The helper must be launched from inside its .app bundle, otherwise macOS
# refuses the audio-capture permission and silently returns zeroed buffers.
TAP_APP = INSTALL_ROOT / "native" / "build" / "CluelessTap.app"
TAP_BINARY = TAP_APP / "Contents" / "MacOS" / "audiotap"

# On-device OCR for screen context (Apple Vision). Plain CLI binary: OCR reads
# an image file and needs no TCC permission of its own. Capturing the screen is
# what needs Screen Recording, and that happens in Electron.
OCR_BINARY = INSTALL_ROOT / "native" / "build" / "ocr"

# Apple Foundation Models bridge (on-device LLM on the Neural Engine).
# Absent or unavailable -> generation falls back to Ollama automatically.
FM_CLI = INSTALL_ROOT / "native" / "build" / "fmcli"

# Per-task backend choice, measured rather than assumed.
#
#   live    -> ollama     Foundation Models abstained on answerable questions
#                         ("what is blocking the release?" -> "No info") and
#                         leaked unrelated instructions into answers. A wrong
#                         answer mid-meeting costs more than the seconds saved.
#   summary -> foundation  Passed every structural and grounding check at 2.8s
#                         versus ~7s, on the Neural Engine, no extra process.
#
# Override with LLM_BACKEND=foundation|ollama to force one for both tasks.
LLM_BACKEND = os.environ.get("LLM_BACKEND", "")
LIVE_BACKEND = os.environ.get("LIVE_BACKEND", LLM_BACKEND or "ollama")
SUMMARY_BACKEND = os.environ.get("SUMMARY_BACKEND", LLM_BACKEND or "foundation")

# Bundle-ID substrings for every app we can follow. Matching is substring-based
# because these apps emit audio from helper processes, not their main PID
# (Teams alone spawns teams2.helper, teams2.modulehost, teams2.notificationcenter).
#
# Order does not matter. Add or remove entries freely — detection, capture and
# the tap all read this one list.
TARGET_APPS = [
    "microsoft.teams2",       # Microsoft Teams
    "cisco-systems.spark",    # Webex (bundle id is Cisco-Systems.Spark)
    "com.apple.facetime",     # FaceTime
    # FaceTime call audio is actually produced by this daemon, not by the
    # FaceTime app itself — without it, detection never fires for a FaceTime call.
    "avconferenced",
    "zoom.us",                # Zoom
    "skype",                  # Skype
]

# Passed to the native helper as one comma-separated argument.
TARGET_APP_MATCH = ",".join(TARGET_APPS)

# --- Speech to text ------------------------------------------------------
# Transcription happens inside the Swift helper using the macOS 26 on-device
# Speech API (SpeechAnalyzer + SpeechTranscriber), not in Python.
#
# faster-whisper was the original plan, but its weights come from Hugging Face,
# which some managed networks block. Apple's speech assets come from Apple's own
# CDN, so the first-party API works with no manual model download.
#
# Measured: 12.6s of speech transcribed in 0.86s (~15x realtime) on Apple silicon,
# fully on-device, and it required no TCC permission prompt.
# Overridable from .env so an accent mismatch can be tested without editing code.
# A locale whose acoustic model was trained on closer accents (en_GB, en_IN,
# en_AU, ...) often transcribes them better, even though the words are the same.
SPEECH_LOCALE = _setting("SPEECH_LOCALE", "en-US")

# Rolling transcript kept in memory only — never written to disk.
TRANSCRIPT_MAX_TURNS = 400

# Deterministic repairs for terms the on-device model reliably mishears.
#
# SpeechTranscriber was chosen over the more accurate DictationTranscriber
# because only it streams clean finalized segments (see native/speech.swift).
# Its cost is domain vocabulary: uncommon names come out spelled three different
# ways across sessions, "read only" comes out "red-only", and those errors
# otherwise propagate into answers and summaries, where they look like facts.
#
# Keys are lower-case and matched on word boundaries, case-insensitively, so
# "red only", "Red only" and "RED ONLY" all map to the same correction.
# Generic corrections only. Names, industry and organisation terms belong in
# CLUELESS_CORRECTIONS (see below) so the shipped defaults are not specific to
# one employer or one field.
SPEECH_CORRECTIONS = {
    "red only": "read only",
    "red-only": "read-only",
    "redonly": "read-only",
    "kafka": "Kafka",
    # Phrase-level: "coffee" and "right" are ordinary words, so only these
    # specific bigrams are corrected. Observed from the microphone path, which
    # picks up speaker audio at lower quality than the system tap.
    "coffee authentication": "Kafka authentication",
    "coffee authentication failure": "Kafka authentication failure",
    "audit right path": "audit write path",
    "right path remediation": "write path remediation",
    "jwt": "JWT",
    # Generic engineering terms the on-device model mangles. Keep these
    # employer-agnostic; org-specific words go in CLUELESS_CORRECTIONS.
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

# User-supplied corrections, e.g. CLUELESS_CORRECTIONS="acme=Acme Corp,dosage=dosage"
# in .env or settings.json (via the settings window). Must use _setting() so the
# UI save path is consulted — os.environ alone ignored everything Settings wrote.
_extra = _setting("CLUELESS_CORRECTIONS", "")
for _pair in _extra.split(","):
    if "=" in _pair:
        _wrong, _right = _pair.split("=", 1)
        if _wrong.strip():
            SPEECH_CORRECTIONS[_wrong.strip().lower()] = _right.strip()

# --- Models --------------------------------------------------------------
# Models must already be present locally (some networks block model registries;
# see docs/providers-and-models.md for offline import). Measured latency:
# llama3.2 1.08s, qwen2.5 1.41s, llama3.1:8b 1.79s.
# llama3.2 and llama3.1 correctly abstain without context; qwen2.5 confabulated
# a confident false answer in testing, which is disqualifying for a RAG tool.
OLLAMA_HOST = "http://127.0.0.1:11434"

# Which local model server answers questions (see server/providers/).
#   auto           first of Ollama, llama.cpp (8080/8081), LM Studio (1234) that responds
#   ollama         Ollama at LLM_BASE_URL or OLLAMA_HOST
#   openai_compat  any /v1/chat/completions server at LLM_BASE_URL
#   llamacpp       Clueless starts llama-server itself for GGUF_PATH
#   foundation     Apple Intelligence on-device model
LLM_PROVIDER = _setting("LLM_PROVIDER", "auto")
LLM_BASE_URL = _setting("LLM_BASE_URL", "")
LLAMACPP_SERVER = _setting("LLAMACPP_SERVER", "")
GGUF_PATH = _setting("GGUF_PATH", "")
LLAMACPP_MANAGED_PORT = 8081
# Provider URLs must be loopback: a remote server would receive the meeting
# transcript. Env-only and deliberately absent from the settings window.
ALLOW_REMOTE_PROVIDER = os.environ.get("ALLOW_REMOTE_PROVIDER", "").lower() in ("1", "true", "yes")

# How long Ollama keeps the model resident after a request.
#
# Summaries now run on the Neural Engine, so Ollama is only used for live
# answers. Holding an 8B model (~5 GB resident, plus GPU allocation) for an idle
# meeting is wasted memory and power. "90s" unloads it a minute and a half after
# the last question, at the cost of a few seconds of reload on the next one.
# Set to "-1" to keep it resident for lowest latency.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "90s")

# Residency while a meeting is in progress. A meeting asks a question every few
# minutes, so the idle value above would evict the model between each one and
# every answer would pay a reload.
OLLAMA_KEEP_ALIVE_IN_MEETING = os.environ.get("OLLAMA_KEEP_ALIVE_IN_MEETING", "30m")
# Measured on a five-question battery over a real transcript:
#   llama3.2 (3B)   4/5 correct, ~1.9s  — too weak: abstained on a question the
#                                        transcript plainly answered
#   llama3.1:8b     5/5 correct, ~3.8s  — chosen
# Correctness matters more than two seconds here: a confidently wrong answer in
# a live meeting is worse than a slightly slower right one.
LIVE_MODEL = _setting("LIVE_MODEL", "llama3.1:8b")
SUMMARY_MODEL = _setting("SUMMARY_MODEL", "llama3.1:8b")
EMBED_MODEL = _setting("EMBED_MODEL", "nomic-embed-text:latest")

# The live prompt measures ~1,200 tokens after trimming, so 8,192 was pure
# over-allocation: Ollama sizes its KV cache to this value, and a smaller one
# allocates and loads faster.
LIVE_NUM_CTX = int(_setting("LIVE_NUM_CTX", "4096"))
# Answers are 1-3 sentences (~40-60 tokens). At 200 the model reliably ran to
# the cap, and generation — not prompt processing — was the latency: measured
# 8.3s of generation for a 3.9k-char prompt. Capping at 90 enforces brevity.
LIVE_NUM_PREDICT = int(_setting("LIVE_NUM_PREDICT", "90"))
# Low temperature: this is a grounding task, not a creative one. Higher values
# produced confident answers assembled from irrelevant retrieved fragments.
LIVE_TEMPERATURE = float(_setting("LIVE_TEMPERATURE", "0.1"))
SUMMARY_NUM_CTX = 16384
SUMMARY_NUM_PREDICT = 900

# --- Retrieval -----------------------------------------------------------
CHROMA_DIR = DATA_DIR / ".chroma"
JIRA_COLLECTION = "jira_issues"
CODE_COLLECTION = "repo_code"
CONFLUENCE_COLLECTION = "confluence_pages"
RETRIEVE_TOP_K = 4

# --- Profile -------------------------------------------------------------
# developer | medical | general. Chosen in onboarding; see server/profiles.py.
_TRUE = ("1", "true", "yes")
_dev_flag = _setting("DEVELOPER_MODE", "").lower() in _TRUE
PROFILE = _profiles.resolve(_setting("PROFILE", ""), _dev_flag)
PROFILE_SETTINGS = _profiles.get(PROFILE)

# Developer mode gates the work-specific sources (Jira, Confluence, code).
#
# For general use — any meeting, any person — those sources are noise at best and
# misleading at worst: the model will happily build an answer out of a ticket
# that happens to share a keyword. The developer profile turns it on; the
# medical profile forbids it regardless of the flag.
DEVELOPER_MODE = PROFILE_SETTINGS["developer_mode"] or (_dev_flag and PROFILE != "medical")

# Whether anything may be fetched from the network (Jira, Confluence, GitHub).
# Off for the medical profile, and not overridable from the UI.
NETWORK_INDEXING = PROFILE_SETTINGS["network_indexing"]

# Redact identifiers (names after titles, dates, record numbers, contact
# details) from everything shown, copied or exported. The raw transcript stays
# in memory for the local model only. On by default for the medical profile.
_redact = _setting("REDACT_PHI", "")
REDACT_PHI = (_redact.lower() in _TRUE) if _redact else PROFILE_SETTINGS["redact_phi"]

# Which sources are consulted, toggleable at runtime (Cmd+Option+1/2/3/4).
# Cmd+Shift+3/4 are macOS screenshot shortcuts and cannot be registered.
#
# Code defaults to OFF. Most meetings discuss tickets, decisions and people
# rather than source files, and a code index that is not relevant to the topic
# does not merely add noise — it actively misleads, because retrieved code
# outranks nothing in particular and the model tries to use it. One keystroke
# turns it back on when the conversation actually turns to implementation.
#
# Toggles rather than automatic relevance gating: similarity scores on this stack
# do not separate relevant from irrelevant ("how do I bake sourdough bread?"
# scored 0.608 against real code), so an automatic gate would be guessing.
DEFAULT_SOURCES = {
    "jira": DEVELOPER_MODE,
    "confluence": DEVELOPER_MODE,
    "code": False,
    # Screen context is generally useful: "what does this slide say" applies to
    # any meeting, not just engineering ones.
    "screen": False,
}

# Sources that only make sense for development work.
DEVELOPER_SOURCES = ("jira", "confluence", "code")

# --- Context sources -----------------------------------------------------
# All organisation-specific values come from .env or the setup wizard. Empty
# means "not configured": the Jira and Confluence indexers skip cleanly instead
# of failing, and the app runs as a general meeting assistant.
JIRA_CLOUD_ID = _setting("JIRA_CLOUD_ID")
JIRA_SITE = _setting("JIRA_SITE")
JIRA_PROJECT_KEY = _setting("JIRA_PROJECT_KEY")
JIRA_BOARD_ID = _setting("JIRA_BOARD_ID")
# Upper bound on indexed issues. The JQL now covers unassigned and recently
# updated work as well as the open sprint, so this needs headroom.
JIRA_MAX_ISSUES = 800

# Confluence spaces to index. Whitelisted rather than the whole instance: large
# sites have thousands of mostly personal spaces, and indexing them all buries
# the relevant pages.
CONFLUENCE_SPACES = _setting_list("CONFLUENCE_SPACES")
CONFLUENCE_MAX_PAGES = 400

# If the org is only visible to a second gh account, set GITHUB_ACCOUNT
# so the indexer authenticates as that account.
GITHUB_ORG = _setting("GITHUB_ORG")
GITHUB_ACCOUNT = _setting("GITHUB_ACCOUNT")
GITHUB_REPOS = _setting_list("GITHUB_REPOS")

# The terraform repo alone is ~75 MB, mostly lockfiles and vendored providers.
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

# Files that embed close to any query but carry no engineering signal. Without
# this filter a "what is blocking the release?" question retrieves i18n strings,
# which crowds out real context and lengthens the prompt.
CODE_LOW_VALUE_MARKERS = (
    "text.json",
    "messages/",
    "/i18n/",
    "/locales/",
    "package-lock.json",
    ".min.",
    "schema.graphql",
)

# Prompt budget. Retrieved code chunks are 120 lines each, so untruncated
# context both swamps the instruction budget and multiplies prompt-processing
# time: an early run took 15.4s for an answer that a 2.5s baseline should give.
CONTEXT_MAX_CHARS_PER_CHUNK = 800
CONTEXT_MAX_TOTAL_CHARS = 2000
# Live answers need to be fast. Measured: prompt evaluation is ~2.0s of a 2.7s
# warm answer, running at ~650 tok/s, so prompt size is the dominant cost. At
# ~1,300 tokens the budget below was 2,700 chars of context plus transcript;
# trimming both to ~1,500 total saves roughly 0.8s per question.
TRANSCRIPT_MAX_CHARS = 1500

# --- Server --------------------------------------------------------------
# Start capture only when the target app is actually producing audio, and stop
# it after the call ends. Recording an idle room wastes power and keeps a ~5 GB
# model warm for nothing; polling CoreAudio's per-process IsRunningOutput is the
# honest signal for "in a call" and needs no extra permission.
AUTO_DETECT_CALLS = _setting("AUTO_DETECT_CALLS", "1").lower() in ("1", "true", "yes")
CALL_POLL_SECONDS = 15
CALL_END_GRACE_SECONDS = 180  # keep capturing through short silences

HOST = "127.0.0.1"
# Not 8765, which other local developer tools commonly use.
PORT = 8766
