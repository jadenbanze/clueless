"""Configuration for Clueless.

Values are deliberately plain module constants: this runs as a single local
process for one user, so there is no need for layered config resolution.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load credentials from .env before anything reads os.environ.
#
# This must happen in config.py, which every other module imports: the API is
# spawned by Electron as a child process, so it does not inherit variables
# exported in a shell. A file on disk is the only way `npm start` sees them.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # python-dotenv is optional; env vars still work
    pass

# Non-secret settings written by the settings window. Kept separate from .env so
# the UI never rewrites a file containing credentials.
VERSION = "0.2.0"

SETTINGS_PATH = PROJECT_ROOT / ".cache" / "settings.json"
try:
    import json as _json

    _saved = _json.loads(SETTINGS_PATH.read_text())
except (OSError, ValueError):
    _saved = {}


def _setting(key: str, default: str = "") -> str:
    return str(_saved.get(key, os.environ.get(key, default)))

# --- Audio capture -------------------------------------------------------
# The helper must be launched from inside its .app bundle, otherwise macOS
# refuses the audio-capture permission and silently returns zeroed buffers.
TAP_APP = PROJECT_ROOT / "native" / "build" / "CluelessTap.app"
TAP_BINARY = TAP_APP / "Contents" / "MacOS" / "audiotap"

# On-device OCR for screen context (Apple Vision). Plain CLI binary: OCR reads
# an image file and needs no TCC permission of its own. Capturing the screen is
# what needs Screen Recording, and that happens in Electron.
OCR_BINARY = PROJECT_ROOT / "native" / "build" / "ocr"

# Apple Foundation Models bridge (on-device LLM on the Neural Engine).
# Absent or unavailable -> generation falls back to Ollama automatically.
FM_CLI = PROJECT_ROOT / "native" / "build" / "fmcli"

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
    "zoom.us",                # Zoom (not installed here)
    "skype",                  # Skype (not installed here)
]

# Passed to the native helper as one comma-separated argument.
TARGET_APP_MATCH = ",".join(TARGET_APPS)

# --- Speech to text ------------------------------------------------------
# Transcription happens inside the Swift helper using the macOS 26 on-device
# Speech API (SpeechAnalyzer + SpeechTranscriber), not in Python.
#
# faster-whisper was the original plan but its weights come from HuggingFace,
# which the corporate network blocks under the same "Generative AI" category as
# the Ollama registry. Apple's asset CDN is reachable, so the first-party API
# works here with no manual model download.
#
# Measured: 12.6s of speech transcribed in 0.86s (~15x realtime) on the M3 Pro,
# fully on-device, and it required no TCC permission prompt.
# Overridable from .env so an accent mismatch can be tested without editing code.
# Installed on this machine: en_US, en_GB, en_AU, en_CA, en_IE, en_IN, en_NZ,
# en_SG. A locale whose acoustic model was trained on closer accents often
# transcribes them better, even though the words are the same.
SPEECH_LOCALE = _setting("SPEECH_LOCALE", "en-US")

# Rolling transcript kept in memory only — never written to disk.
TRANSCRIPT_MAX_TURNS = 400

# Deterministic repairs for terms the on-device model reliably mishears.
#
# SpeechTranscriber was chosen over the more accurate DictationTranscriber
# because only it streams clean finalized segments (see native/speech.swift).
# Its cost is domain vocabulary: "Shweta" comes out "Shwada", "read only" comes
# out "red-only", and those errors otherwise propagate into answers and
# summaries, where they look like real facts.
#
# Keys are lower-case and matched on word boundaries, case-insensitively, so
# "shwada", "Shwada" and "SHWADA" all map to the same correction. Extend this
# from Jira summaries and repo identifiers once the Jira index is populated.
# Generic corrections only. Industry and organisation terms belong in
# CLUELESS_CORRECTIONS (see below) so the shipped defaults are not specific to
# one employer or one field.
SPEECH_CORRECTIONS = {
    # Observed renderings of the same name across sessions.
    "shwada": "Shweta",
    "schweda": "Shweta",
    "shweda": "Shweta",
    "shweta's": "Shweta's",
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
}

# User-supplied corrections, e.g. CLUELESS_CORRECTIONS="acme=Acme Corp,dosage=dosage"
# in .env, or edited from the settings window. This is how a medical, legal or
# finance user teaches the transcriber their vocabulary without touching code.
_extra = os.environ.get("CLUELESS_CORRECTIONS", "")
for _pair in _extra.split(","):
    if "=" in _pair:
        _wrong, _right = _pair.split("=", 1)
        if _wrong.strip():
            SPEECH_CORRECTIONS[_wrong.strip().lower()] = _right.strip()

# --- Models --------------------------------------------------------------
# The corporate network blocks registry.ollama.ai under a "Generative AI"
# category deny, so these must already be present locally. Measured latency:
# llama3.2 1.08s, qwen2.5 1.41s, llama3.1:8b 1.79s.
# llama3.2 and llama3.1 correctly abstain without context; qwen2.5 confabulated
# a confident false answer in testing, which is disqualifying for a RAG tool.
OLLAMA_HOST = "http://127.0.0.1:11434"

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
LIVE_MODEL = "llama3.1:8b"
SUMMARY_MODEL = "llama3.1:8b"
EMBED_MODEL = "nomic-embed-text:latest"

# The live prompt measures ~1,200 tokens after trimming, so 8,192 was pure
# over-allocation: Ollama sizes its KV cache to this value, and a smaller one
# allocates and loads faster.
LIVE_NUM_CTX = 4096
# Answers are 1-3 sentences (~40-60 tokens). At 200 the model reliably ran to
# the cap, and generation — not prompt processing — was the latency: measured
# 8.3s of generation for a 3.9k-char prompt. Capping at 90 enforces brevity.
LIVE_NUM_PREDICT = 90
# Low temperature: this is a grounding task, not a creative one. Higher values
# produced confident answers assembled from irrelevant retrieved fragments.
LIVE_TEMPERATURE = 0.1
SUMMARY_NUM_CTX = 16384
SUMMARY_NUM_PREDICT = 900

# --- Retrieval -----------------------------------------------------------
CHROMA_DIR = PROJECT_ROOT / ".chroma"
JIRA_COLLECTION = "jira_issues"
CODE_COLLECTION = "repo_code"
CONFLUENCE_COLLECTION = "confluence_pages"
RETRIEVE_TOP_K = 4

# Developer mode gates the work-specific sources (Jira, Confluence, code).
#
# For general use — any meeting, any person — those sources are noise at best and
# misleading at worst: the model will happily build an answer out of a ticket
# that happens to share a keyword. Off by default; enable with DEVELOPER_MODE=1
# in .env (or the menu bar) when working on the codebase.
# Read through _setting() so the settings window can control it. Reading only
# os.environ meant the UI toggle saved a value nothing ever consulted.
DEVELOPER_MODE = _setting("DEVELOPER_MODE", "").lower() in ("1", "true", "yes")

# Which sources are consulted, toggleable at runtime (Cmd+Shift+1/2/3).
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

# Confluence spaces to index. Not the whole instance: it has thousands of
# spaces, mostly personal, and indexing them would repeat the Jira mistake of
# burying relevant pages. RFM is "Restaurant File Maintenance".
CONFLUENCE_SPACES = ["RFM"]
CONFLUENCE_MAX_PAGES = 400

# If the org is only visible to a second gh account, set GITHUB_ACCOUNT
# so the indexer authenticates as that account.
GITHUB_ORG = _setting("GITHUB_ORG")
GITHUB_ACCOUNT = _setting("GITHUB_ACCOUNT")
GITHUB_REPOS = [r.strip() for r in os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]

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
AUTO_DETECT_CALLS = os.environ.get("AUTO_DETECT_CALLS", "1").lower() in ("1", "true", "yes")
CALL_POLL_SECONDS = 15
CALL_END_GRACE_SECONDS = 180  # keep capturing through short silences

HOST = "127.0.0.1"
# 8765 is already bound by an unrelated local Python service on this machine.
PORT = 8766
