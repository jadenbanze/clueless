"""Validated shape of everything the settings window may write.

`POST /settings` validates every key at the boundary before writing settings.json
since values feed subprocess paths and outbound indexer URLs. Unknown keys are
rejected so typos surface immediately.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from .security import is_loopback_url

_LIST_ITEM = re.compile(r"^[A-Za-z0-9._~/-]{1,120}$")


def _match(pattern: str, value: str, what: str) -> str:
    if value and not re.fullmatch(pattern, value):
        raise ValueError(f"invalid {what}")
    return value


def _single_line(value: str, what: str, max_len: int = 240) -> str:
    if not value:
        return value
    if any(ch in value for ch in "\n\r\x00") or len(value) > max_len:
        raise ValueError(f"invalid {what}")
    return value


def _csv(value: str, what: str, limit: int = 50) -> str:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if len(items) > limit:
        raise ValueError(f"too many {what} (max {limit})")
    for item in items:
        if not _LIST_ITEM.fullmatch(item):
            raise ValueError(f"invalid {what}: {item[:40]!r}")
    return ",".join(items)


def _bounded_number(value: str, low: float, high: float, what: str, integer: bool = True) -> str:
    if not value:
        return value
    try:
        number = int(value) if integer else float(value)
    except ValueError:
        raise ValueError(f"{what} must be a number") from None
    if not low <= number <= high:
        raise ValueError(f"{what} must be between {low} and {high}")
    return str(number)


def _path(value: str, what: str, suffixes: tuple[str, ...] | None = None) -> str:
    if not value:
        return value
    if "\n" in value or "\x00" in value or len(value) > 1024:
        raise ValueError(f"invalid {what}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{what} must be an absolute path")
    if suffixes and path.suffix.lower() not in suffixes:
        allowed = " or ".join(suffixes)
        raise ValueError(f"{what} must be a {allowed} file")
    return str(path)


def _path_list(value: str, what: str, limit: int = 20) -> str:
    if not value:
        return value
    raw_items = [p.strip() for p in re.split(r"[;\n,]", value) if p.strip()]
    if len(raw_items) > limit:
        raise ValueError(f"too many {what} (max {limit})")
    cleaned = [_path(item, what) for item in raw_items]
    return ";".join(cleaned)


def _https_site(value: str, what: str) -> str:
    if not value:
        return value
    candidate = value if "://" in value else f"https://{value}"
    parts = urlsplit(candidate)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host or "." not in host or any(ch in value for ch in "\n\r\x00 "):
        raise ValueError(f"{what} must be a valid HTTPS domain")
    base = f"https://{host}" + (f":{parts.port}" if parts.port else "")
    return base.rstrip("/")


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, str_max_length=4000)

    # Profile, personal context, and onboarding
    PROFILE: str | None = None
    CLINICAL_MODE: str | None = None
    ONBOARDING_COMPLETE: str | None = None
    ONBOARDING_STEP: str | None = None
    REDACT_PHI: str | None = None
    AUTO_DETECT_CALLS: str | None = None
    DEVELOPER_MODE: str | None = None
    USER_NAME: str | None = None
    USER_ROLE: str | None = None
    USER_FOCUS: str | None = None

    # Speech
    SPEECH_LOCALE: str | None = None
    CLUELESS_CORRECTIONS: str | None = None

    # Model provider
    LLM_PROVIDER: str | None = None
    LLM_BASE_URL: str | None = None
    LIVE_MODEL: str | None = None
    SUMMARY_MODEL: str | None = None
    EMBED_MODEL: str | None = None
    LLAMACPP_SERVER: str | None = None
    GGUF_PATH: str | None = None
    LIVE_NUM_CTX: str | None = None
    LIVE_NUM_PREDICT: str | None = None
    LIVE_TEMPERATURE: str | None = None

    # Universal, Student & Research context sources
    LOCAL_NOTES_DIRS: str | None = None
    ANKI_PATH: str | None = None
    ANKI_DECK_FILTER: str | None = None
    PUBMED_QUERY: str | None = None
    ZOTERO_BIB_PATH: str | None = None
    CANVAS_URL: str | None = None
    NOTION_QUERY: str | None = None
    CALENDAR_ENABLED: str | None = None

    # Developer context sources
    JIRA_SITE: str | None = None
    JIRA_PROJECT_KEY: str | None = None
    JIRA_BOARD_ID: str | None = None
    JIRA_CLOUD_ID: str | None = None
    CONFLUENCE_SPACES: str | None = None
    GITHUB_ORG: str | None = None
    GITHUB_ACCOUNT: str | None = None
    GITHUB_REPOS: str | None = None

    @field_validator("PROFILE")
    @classmethod
    def _profile(cls, v):
        return _match(r"developer|student|medical|general", v, "profile") if v is not None else v

    @field_validator(
        "ONBOARDING_COMPLETE", "REDACT_PHI", "AUTO_DETECT_CALLS",
        "DEVELOPER_MODE", "CLINICAL_MODE", "CALENDAR_ENABLED",
    )
    @classmethod
    def _flag(cls, v):
        if v is None or v == "":
            return v
        if v.lower() not in ("0", "1", "true", "false", "yes", "no"):
            raise ValueError("must be a boolean flag")
        return "1" if v.lower() in ("1", "true", "yes") else "0"

    @field_validator("ONBOARDING_STEP")
    @classmethod
    def _step(cls, v):
        return _bounded_number(v, 0, 20, "onboarding step") if v is not None else v

    @field_validator("USER_NAME")
    @classmethod
    def _user_name(cls, v):
        return _single_line(v, "name", 80) if v is not None else v

    @field_validator("USER_ROLE")
    @classmethod
    def _user_role(cls, v):
        return _single_line(v, "role", 120) if v is not None else v

    @field_validator("USER_FOCUS")
    @classmethod
    def _user_focus(cls, v):
        return _single_line(v, "focus", 240) if v is not None else v

    @field_validator("SPEECH_LOCALE")
    @classmethod
    def _locale(cls, v):
        return _match(r"[a-z]{2,3}[-_][A-Z]{2}", v, "locale") if v is not None else v

    @field_validator("CLUELESS_CORRECTIONS")
    @classmethod
    def _corrections(cls, v):
        return _single_line(v, "corrections", 4000) if v is not None else v

    @field_validator("LLM_PROVIDER")
    @classmethod
    def _provider(cls, v):
        return _match(r"auto|ollama|openai_compat|llamacpp|foundation", v, "provider") if v is not None else v

    @field_validator("LLM_BASE_URL")
    @classmethod
    def _base_url(cls, v):
        if v and not is_loopback_url(v):
            raise ValueError("provider URL must be on this machine (127.0.0.1 or localhost)")
        return v

    @field_validator("LIVE_MODEL", "SUMMARY_MODEL", "EMBED_MODEL")
    @classmethod
    def _model(cls, v):
        return _match(r"[A-Za-z0-9._:/-]{1,120}", v, "model name") if v is not None else v

    @field_validator("LLAMACPP_SERVER")
    @classmethod
    def _server_bin(cls, v):
        return _path(v, "llama-server path") if v is not None else v

    @field_validator("GGUF_PATH")
    @classmethod
    def _gguf(cls, v):
        return _path(v, "GGUF path", (".gguf",)) if v is not None else v

    @field_validator("LOCAL_NOTES_DIRS")
    @classmethod
    def _notes_dirs(cls, v):
        return _path_list(v, "local notes folders") if v is not None else v

    @field_validator("ANKI_PATH")
    @classmethod
    def _anki_path(cls, v):
        if v is None or v == "" or v.lower() == "auto":
            return "auto" if v and v.lower() == "auto" else (v or "")
        return _path(v, "Anki path", (".apkg", ".anki2", ".anki21", ".sqlite", ".sqlite3", ".db"))

    @field_validator("ANKI_DECK_FILTER", "NOTION_QUERY")
    @classmethod
    def _short_filter(cls, v):
        return _single_line(v, "filter", 120) if v is not None else v

    @field_validator("PUBMED_QUERY")
    @classmethod
    def _pubmed(cls, v):
        return _single_line(v, "PubMed query", 240) if v is not None else v

    @field_validator("ZOTERO_BIB_PATH")
    @classmethod
    def _zotero(cls, v):
        return _path(v, "Zotero bibliography", (".bib", ".ris")) if v is not None else v

    @field_validator("CANVAS_URL")
    @classmethod
    def _canvas(cls, v):
        return _https_site(v, "Canvas URL") if v is not None else v

    @field_validator("LIVE_NUM_CTX")
    @classmethod
    def _ctx(cls, v):
        return _bounded_number(v, 1024, 32768, "context size") if v is not None else v

    @field_validator("LIVE_NUM_PREDICT")
    @classmethod
    def _predict(cls, v):
        return _bounded_number(v, 16, 1024, "max answer tokens") if v is not None else v

    @field_validator("LIVE_TEMPERATURE")
    @classmethod
    def _temp(cls, v):
        return _bounded_number(v, 0, 1.5, "temperature", integer=False) if v is not None else v

    @field_validator("JIRA_SITE")
    @classmethod
    def _site(cls, v):
        return _match(r"[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net", v, "Jira site") if v is not None else v

    @field_validator("JIRA_PROJECT_KEY")
    @classmethod
    def _key(cls, v):
        return _match(r"[A-Z][A-Z0-9_]{1,19}", v, "project key") if v is not None else v

    @field_validator("JIRA_BOARD_ID")
    @classmethod
    def _board(cls, v):
        return _match(r"[0-9]{1,10}", v, "board id") if v is not None else v

    @field_validator("JIRA_CLOUD_ID")
    @classmethod
    def _cloud(cls, v):
        return _match(r"[0-9a-f-]{36}", v, "cloud id") if v is not None else v

    @field_validator("GITHUB_ORG", "GITHUB_ACCOUNT")
    @classmethod
    def _gh(cls, v):
        return _match(r"[A-Za-z0-9_-]{1,39}", v, "GitHub name") if v is not None else v

    @field_validator("CONFLUENCE_SPACES")
    @classmethod
    def _spaces(cls, v):
        return _csv(v, "Confluence spaces") if v is not None else v

    @field_validator("GITHUB_REPOS")
    @classmethod
    def _repos(cls, v):
        return _csv(v, "repositories") if v is not None else v


SETTING_KEYS = tuple(SettingsUpdate.model_fields)


# Credentials written exclusively to DATA_DIR/.env (mode 0600), never settings.json
# and never echoed back over HTTP.
SECRET_KEYS = (
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_API_TOKEN",
    "GITHUB_TOKEN",
    "CANVAS_TOKEN",
    "NOTION_TOKEN",
    "NCBI_API_KEY",
)


class CredentialsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, str_max_length=1000)

    ATLASSIAN_EMAIL: str | None = None
    ATLASSIAN_API_TOKEN: str | None = None
    GITHUB_TOKEN: str | None = None
    CANVAS_TOKEN: str | None = None
    NOTION_TOKEN: str | None = None
    NCBI_API_KEY: str | None = None

    @field_validator(*SECRET_KEYS)
    @classmethod
    def _no_newlines(cls, v):
        if v is not None and any(ch in v for ch in "\n\r\x00"):
            raise ValueError("credential must be a single line")
        return v
