"""The only HTTP client model providers use.

Every request goes through `_open`, which refuses anything but http(s) to this
machine. That one check is what keeps the transcript local: a provider URL
typed into Settings, or one injected into settings.json, cannot point a prompt
at a remote server. ALLOW_REMOTE_PROVIDER (env only) is the documented opt-out.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator

from .. import config
from ..security import is_loopback_url
from .base import ProviderError


def check_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise ProviderError(f"refusing non-HTTP provider URL: {url[:60]}")
    if not config.ALLOW_REMOTE_PROVIDER and not is_loopback_url(url):
        raise ProviderError("provider URL is not on this machine; set ALLOW_REMOTE_PROVIDER=1 to allow it")


def _open(url: str, payload: dict | None, timeout: float):
    check_url(url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, data=data, method="GET" if payload is None else "POST",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        # Scheme and host are validated by check_url above.
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosec B310
    except urllib.error.HTTPError as error:
        detail = error.read(400).decode("utf-8", "replace")
        raise ProviderError(f"HTTP {error.code} from {url}: {detail}") from None
    except (urllib.error.URLError, OSError) as error:
        raise ProviderError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from None


def get_json(url: str, timeout: float = 3.0) -> dict:
    with _open(url, None, timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: float = 90.0) -> dict:
    with _open(url, payload, timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_lines(url: str, payload: dict, timeout: float = 120.0) -> Iterator[str]:
    """Yields non-empty response lines (NDJSON or SSE) as they arrive."""
    with _open(url, payload, timeout) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if line:
                yield line


def reachable(url: str, timeout: float = 0.3) -> bool:
    try:
        with _open(url, None, timeout) as response:
            return 200 <= response.status < 300
    except ProviderError:
        return False
