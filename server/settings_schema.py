"""Validated shape of everything the settings window may write.

`POST /settings` used to accept an arbitrary dict and stringify each value into
settings.json. Every key here is read back by config.py and some reach
subprocess arguments (GGUF path, llama-server binary) or outbound URLs (Jira
site, provider base URL), so each is validated at the boundary rather than
trusted later.

Unknown keys are rejected, not ignored, so a typo surfaces as an error.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from .security import is_loopback_url

_LIST_ITEM = re.compile(r"^[A-Za-z0-9._~-]{1,100}$")


def _match(pattern: str, value: str, what: str) -> str:
    if value and not re.fullmatch(pattern, value):
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


def _path(value: str, what: str, suffix: str | None = None) -> str:
    if not value:
        return value
    if "\n" in value or "\x00" in value or len(value) > 1024:
        raise ValueError(f"invalid {what}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{what} must be an absolute path")
    if suffix and path.suffix.lower() != suffix:
        raise ValueError(f"{what} must be a {suffix} file")
    return str(path)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, str_max_length=4000)

    # Profile and onboarding
    PROFILE: str | None = None
    ONBOARDING_COMPLETE: str | None = None
    ONBOARDING_STEP: str | None = None
    REDACT_PHI: str | None = None
    AUTO_DETECT_CALLS: str | None = None
    DEVELOPER_MODE: str | None = None

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

    # Work sources (developer profile)
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
        return _match(r"developer|medical|general", v, "profile") if v is not None else v

    @field_validator("ONBOARDING_COMPLETE", "REDACT_PHI", "AUTO_DETECT_CALLS", "DEVELOPER_MODE")
    @classmethod
    def _flag(cls, v):
        if v is None or v == "":
            return v
        if v.lower() not in ("0", "1", "true", "false", "yes", "no"):
            raise ValueError("must be a boolean flag")
        # An explicit "0", not "": a saved "" means "use the default", so a
        # flag whose default is on could never be switched off.
        return "1" if v.lower() in ("1", "true", "yes") else "0"

    @field_validator("ONBOARDING_STEP")
    @classmethod
    def _step(cls, v):
        return _bounded_number(v, 0, 20, "onboarding step") if v is not None else v

    @field_validator("SPEECH_LOCALE")
    @classmethod
    def _locale(cls, v):
        return _match(r"[a-z]{2,3}[-_][A-Z]{2}", v, "locale") if v is not None else v

    @field_validator("CLUELESS_CORRECTIONS")
    @classmethod
    def _corrections(cls, v):
        if v is None:
            return v
        if any(ch in v for ch in "\n\r\x00"):
            raise ValueError("corrections must be a single line")
        return v

    @field_validator("LLM_PROVIDER")
    @classmethod
    def _provider(cls, v):
        return _match(r"auto|ollama|openai_compat|llamacpp|foundation", v, "provider") if v is not None else v

    @field_validator("LLM_BASE_URL")
    @classmethod
    def _base_url(cls, v):
        # Local-only is the product's core promise; a remote URL would send the
        # transcript off the machine. config.ALLOW_REMOTE_PROVIDER is the
        # documented, env-only escape hatch and cannot be set from the UI.
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
        return _path(v, "GGUF path", ".gguf") if v is not None else v

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
