"""Loopback API guard: every way a browser page could reach the API is refused.

Run: python3 -m server.test_security
"""

from __future__ import annotations

from .security import allowed_hosts, check_request

TOKEN = "a" * 64
HOSTS = allowed_hosts(8766)


def scope(kind="http", path="/health", **headers):
    return {
        "type": kind,
        "path": path,
        "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
    }


def status(s):
    result = check_request(s, TOKEN, HOSTS)
    return 200 if result is None else result[0]


CASES = [
    # (description, scope, expected status)
    ("valid bearer", scope(host="127.0.0.1:8766", authorization=f"Bearer {TOKEN}"), 200),
    ("localhost host ok", scope(host="localhost:8766", authorization=f"Bearer {TOKEN}"), 200),
    ("no token", scope(host="127.0.0.1:8766"), 401),
    ("wrong token", scope(host="127.0.0.1:8766", authorization="Bearer nope"), 401),
    ("prefix of token", scope(host="127.0.0.1:8766", authorization=f"Bearer {TOKEN[:-1]}"), 401),
    ("DNS rebinding host", scope(host="evil.example:8766", authorization=f"Bearer {TOKEN}"), 421),
    ("missing host", scope(authorization=f"Bearer {TOKEN}"), 421),
    ("browser origin, even with token",
     scope(host="127.0.0.1:8766", origin="https://evil.example", authorization=f"Bearer {TOKEN}"), 403),
    ("null origin (sandboxed iframe)",
     scope(host="127.0.0.1:8766", origin="null", authorization=f"Bearer {TOKEN}"), 403),
    ("websocket with subprotocol token",
     scope("websocket", "/events", host="127.0.0.1:8766", sec_websocket_protocol=f"clueless.{TOKEN}"), 200),
    ("websocket from a web page",
     scope("websocket", "/events", host="127.0.0.1:8766", origin="http://localhost:5500"), 403),
    ("websocket without token", scope("websocket", "/events", host="127.0.0.1:8766"), 401),
    ("livez needs no token", scope(path="/livez", host="127.0.0.1:8766"), 200),
    ("livez still rejects browsers", scope(path="/livez", host="127.0.0.1:8766", origin="https://x"), 403),
]


def main() -> int:
    failures = 0
    for description, s, expected in CASES:
        got = status(s)
        ok = got == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {description}: {got} (expected {expected})")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
