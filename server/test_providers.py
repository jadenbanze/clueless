"""Provider matrix: the fallback ladder against fake local servers.

Starts throwaway HTTP servers on loopback that speak just enough of the Ollama
and OpenAI-compatible APIs, then checks which provider and model the ladder
picks in each situation, that streaming works for both wire formats, and that
a remote URL is refused before any connection is made.

Run: python3 -m server.test_providers
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, providers
from .providers import http as provider_http
from .providers import ollama as ollama_provider
from .providers.base import ProviderError

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def fake_server(kind: str, models: list[tuple[str, int]]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if kind == "ollama" and self.path == "/api/tags":
                self._send(json.dumps({"models": [{"name": n, "size": s} for n, s in models]}).encode())
            elif kind == "openai" and self.path == "/v1/models":
                self._send(json.dumps({"data": [{"id": n} for n, _ in models]}).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/chat":
                if body.get("stream"):
                    lines = [{"message": {"content": "Hello "}}, {"message": {"content": "there"}},
                             {"done": True, "eval_count": 2}]
                    self._send("\n".join(json.dumps(x) for x in lines).encode(), "application/x-ndjson")
                else:
                    self._send(json.dumps({"message": {"content": f"ollama:{body['model']}"}}).encode())
            elif self.path == "/api/embed":
                self._send(json.dumps({"embeddings": [[0.1, 0.2] for _ in body["input"]]}).encode())
            elif self.path == "/v1/chat/completions":
                if body.get("stream"):
                    events = [{"choices": [{"delta": {"content": "Hi "}}]},
                              {"choices": [{"delta": {"content": "back"}}]}]
                    payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
                    self._send(payload.encode(), "text/event-stream")
                else:
                    self._send(json.dumps({"choices": [{"message": {"content": f"openai:{body['model']}"}}]}).encode())
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def scenario(candidates, *, foundation: bool, provider: str = "auto", live_model: str = "llama3.1:8b"):
    providers.AUTO_CANDIDATES[:] = candidates
    providers._foundation._available = foundation
    config.LLM_PROVIDER = provider
    config.LIVE_MODEL = live_model
    return providers.resolve(refresh=True)


def main() -> int:
    saved = (list(providers.AUTO_CANDIDATES), config.LLM_PROVIDER, config.LIVE_MODEL, config.LLM_BASE_URL,
             providers._foundation._available)
    # The "largest model that fits in half of RAM" expectations below were written
    # for a 36 GB Mac. Pin RAM so the result does not depend on the machine running
    # the tests (CI runners have far less memory).
    saved_ram = ollama_provider._total_ram_bytes
    ollama_provider._total_ram_bytes = lambda: 36 * 1024**3
    ollama, ollama_url = fake_server("ollama", [
        ("llama3.2:latest", 2_000_000_000), ("llama3.1:8b", 4_900_000_000), ("nomic-embed-text:latest", 270_000_000)])
    small_ollama, small_url = fake_server("ollama", [
        ("llama3.2:latest", 2_000_000_000), ("qwen2.5:14b", 9_000_000_000), ("nomic-embed-text:latest", 1),
        ("llama3.3:latest", 900_000_000_000)])
    lmstudio, lm_url = fake_server("openai", [("qwen2.5-7b-instruct", 0), ("text-embedding-nomic", 0)])
    dead = "http://127.0.0.1:9"  # discard port: nothing listens

    try:
        print("ladder")
        r = scenario([("ollama", "ollama", "Ollama", ollama_url)], foundation=True)
        check("configured model installed → ok", r.status == "ok" and r.model == "llama3.1:8b", str(r.public()))
        check("embeddings found on Ollama", r.embed_model == "nomic-embed-text:latest", str(r.embed_model))

        r = scenario([("ollama", "ollama", "Ollama", small_url)], foundation=True)
        check("model missing → largest chat model", r.status == "degraded" and r.model == "qwen2.5:14b", str(r.public()))
        check("substitution is named", "qwen2.5:14b" in r.message and "llama3.1:8b" in r.message, r.message)
        check("embedding model never chosen for chat", "embed" not in (r.model or ""))
        check("model larger than half of RAM never substituted", r.model != "llama3.3:latest")

        r = scenario([("ollama", "ollama", "Ollama", dead), ("lmstudio", "openai_compat", "LM Studio", lm_url)],
                     foundation=True)
        check("auto skips a dead server and finds LM Studio", r.provider is not None and r.provider.id == "lmstudio",
              str(r.public()))
        check("LM Studio serves its loaded model", r.model == "qwen2.5-7b-instruct", str(r.model))
        check("LM Studio embedding model detected", r.embed_model == "text-embedding-nomic", str(r.embed_model))

        r = scenario([("ollama", "ollama", "Ollama", dead)], foundation=True)
        check("no server → Apple Intelligence, degraded", r.status == "degraded" and r.provider.id == "foundation",
              str(r.public()))
        check("degraded message warns about reliability", "less reliable" in r.message, r.message)
        check("no embeddings → retrieval disabled", r.embed_model is None)

        r = scenario([("ollama", "ollama", "Ollama", dead)], foundation=False)
        check("nothing at all → none with fixes", r.status == "none" and len(r.fixes) >= 2, str(r.public()))

        config.LLM_BASE_URL = lm_url
        r = scenario([("ollama", "ollama", "Ollama", ollama_url)], foundation=True, provider="openai_compat")
        check("explicit provider wins over auto order", r.provider.kind == "openai_compat", str(r.public()))
        config.LLM_BASE_URL = ""

        r = scenario([("ollama", "ollama", "Ollama", ollama_url)], foundation=True, provider="foundation")
        check("explicit foundation honoured", r.provider.id == "foundation" and r.status == "ok", str(r.public()))

        print("wire formats")
        scenario([("ollama", "ollama", "Ollama", ollama_url)], foundation=False)
        streamed = "".join(providers.chat_stream("s", "p", max_tokens=8))
        check("Ollama NDJSON stream", streamed == "Hello there", streamed)
        check("Ollama chat", providers.chat("s", "p", max_tokens=8) == "ollama:llama3.1:8b")
        check("Ollama embed batch", len(providers.embed(["a", "b", "c"])) == 3)

        scenario([("lmstudio", "openai_compat", "LM Studio", lm_url)], foundation=False)
        streamed = "".join(providers.chat_stream("s", "p", max_tokens=8))
        check("OpenAI SSE stream", streamed == "Hi back", streamed)

        print("egress guard")
        for url in ("http://example.com/api/tags", "http://10.0.0.5:11434/api/tags", "file:///etc/passwd",
                    "ftp://127.0.0.1/x", "http://127.0.0.1.evil.com/api"):
            try:
                provider_http.check_url(url)
                check(f"refuses {url}", False)
            except ProviderError:
                check(f"refuses {url}", True)
        for url in ("http://127.0.0.1:11434/api/tags", "http://localhost:1234/v1/models", "http://[::1]:8080/v1/models"):
            try:
                provider_http.check_url(url)
                check(f"allows {url}", True)
            except ProviderError as error:
                check(f"allows {url}", False, str(error))
    finally:
        for server in (ollama, small_ollama, lmstudio):
            server.shutdown()
        providers.AUTO_CANDIDATES[:] = saved[0]
        config.LLM_PROVIDER, config.LIVE_MODEL, config.LLM_BASE_URL = saved[1], saved[2], saved[3]
        providers._foundation._available = saved[4]
        ollama_provider._total_ram_bytes = saved_ram

    print(f"\n{'PASS' if not failures else f'FAIL ({len(failures)})'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
