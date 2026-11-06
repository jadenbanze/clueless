"""Access control for the loopback API.

Binding to 127.0.0.1 is not access control. Any web page open in a browser can
reach loopback, and WebSockets are not subject to CORS: without this module a
page could open ws://127.0.0.1:8766/events and read the live transcript, or
POST /settings. DNS rebinding extends the same attack to hostnames.

Three checks, applied to HTTP and WebSocket alike:

1. Host must be the loopback address and port we bound. Defeats DNS rebinding,
   where the browser believes it is talking to attacker.example.
2. Origin must be absent. Every legitimate client (Electron's main process,
   curl, the test harness) sends none; browsers always send one cross-origin.
3. A per-launch bearer token. Electron generates it and passes it through the
   environment (never argv, which `ps` exposes). HTTP sends
   ``Authorization: Bearer <token>``; WebSockets cannot set headers from the
   browser API, so they offer it as the subprotocol ``clueless.<token>``.

When started without a token (``python3 -m server.api`` by hand), one is
generated and written to ``DATA_DIR/.cache/api-token`` with mode 0600 so local
tooling can still authenticate.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from pathlib import Path

from . import config

log = logging.getLogger("security")

# The name of an environment variable, not a secret.
TOKEN_ENV = "CLUELESS_API_TOKEN"  # noqa: S105  # nosec B105
SUBPROTOCOL_PREFIX = "clueless."
# Liveness only, and reveals nothing beyond "something is listening", which a
# port scan shows anyway. Lets scripts wait for boot without the token.
UNAUTHENTICATED_PATHS = frozenset({"/livez"})


def token_path() -> Path:
    return config.DATA_DIR / ".cache" / "api-token"


def load_or_create_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if len(token) >= 32:
        return token
    token = secrets.token_hex(32)
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start rather than chmod after: no window in
    # which another local user could read it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    log.info("no %s in environment; generated one at %s", TOKEN_ENV, path)
    return token


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


def _header(scope: dict, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def check_request(scope: dict, token: str, hosts: frozenset[str]) -> tuple[int, str] | None:
    """Returns (status, reason) to reject, or None to allow."""
    host = _header(scope, b"host")
    if host not in hosts:
        return 421, "misdirected request"
    if _header(scope, b"origin") is not None:
        return 403, "browser origins are not allowed"
    if scope.get("path") in UNAUTHENTICATED_PATHS:
        return None

    presented = ""
    if scope["type"] == "websocket":
        offered = _header(scope, b"sec-websocket-protocol") or ""
        for proto in (p.strip() for p in offered.split(",")):
            if proto.startswith(SUBPROTOCOL_PREFIX):
                presented = proto[len(SUBPROTOCOL_PREFIX):]
                break
    else:
        auth = _header(scope, b"authorization") or ""
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()

    # Constant-time compare: the token is the only secret here.
    if not presented or not hmac.compare_digest(presented, token):
        return 401, "missing or invalid token"
    return None


class LoopbackGuard:
    """Pure ASGI middleware so it covers WebSockets, which HTTP middleware does not."""

    def __init__(self, app, token: str, port: int):
        self.app = app
        self.token = token
        self.hosts = allowed_hosts(port)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        rejection = check_request(scope, self.token, self.hosts)
        if rejection is None:
            await self.app(scope, receive, send)
            return

        status, reason = rejection
        log.warning("rejected %s %s: %s", scope["type"], scope.get("path"), reason)
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": reason}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_url(url: str) -> bool:
    """True for http(s) URLs whose host is this machine.

    Used for every model-provider URL: the product promise is that meeting text
    never leaves the machine, so a remote provider must be an explicit opt-in.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and (parts.hostname or "") in _LOOPBACK_HOSTS


def websocket_subprotocol(scope: dict) -> str | None:
    """The subprotocol to echo on accept; browsers drop the socket otherwise."""
    offered = _header(scope, b"sec-websocket-protocol") or ""
    for proto in (p.strip() for p in offered.split(",")):
        if proto.startswith(SUBPROTOCOL_PREFIX):
            return proto
    return None
