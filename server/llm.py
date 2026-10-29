"""Generation backend: Apple Foundation Models, falling back to Ollama.

Foundation Models (macOS 26) runs Apple's on-device model on the Neural Engine.
Measured against llama3.1:8b on the same grounded-question battery:

    Foundation Models   4/4 correct, avg  874ms, no process, 0 GB disk
    llama3.1:8b         5/5 correct, avg 3823ms, server + ~5 GB

Both are fully local. Foundation Models is preferred because it is markedly
faster and lower-overhead, and because it removes a long-running process and its
memory footprint. Ollama stays as a fallback for machines without Apple
Intelligence enabled, and is still used for embeddings, where its model is
stronger than anything the system provides.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Iterator

from . import config, ollama_client

log = logging.getLogger(__name__)

_backend_choice: str | None = None


def _probe_foundation_models() -> bool:
    """Runs one trivial request to confirm the model is usable."""
    if not config.FM_CLI.exists():
        return False
    try:
        result = subprocess.run(
            [str(config.FM_CLI)],
            input=json.dumps(
                {"instructions": "Reply with one word.", "prompt": "Say ok.", "maxTokens": 8}
            ),
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.debug("foundation models probe failed: %s", error)
        return False

    return '"done":true' in result.stdout.replace(" ", "")


def backend() -> str:
    """Returns 'foundation' or 'ollama', probing once and caching."""
    global _backend_choice
    if _backend_choice is None:
        if _probe_foundation_models():
            _backend_choice = "foundation"
            log.info("generation backend: Apple Foundation Models (on-device, ANE)")
        else:
            _backend_choice = "ollama"
            log.info("generation backend: Ollama (%s)", config.LIVE_MODEL)
    return _backend_choice


def _foundation_stream(instructions: str, prompt: str, max_tokens: int) -> Iterator[str]:
    """Streams fragments from the Foundation Models CLI bridge."""
    request = json.dumps(
        {"instructions": instructions, "prompt": prompt, "maxTokens": max_tokens}
    )
    process = subprocess.Popen(
        [str(config.FM_CLI)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdin and process.stdout
        process.stdin.write(request)
        process.stdin.close()

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in event:
                raise RuntimeError(event["error"])
            if "reset" in event:
                # Non-prefix change; caller replaces rather than appends.
                yield event["reset"]
            elif "delta" in event:
                yield event["delta"]
            if event.get("done"):
                return
    finally:
        process.wait(timeout=30)


def stream(
    system: str, prompt: str, *, max_tokens: int, temperature: float = 0.1
) -> Iterator[str]:
    """Streams a live answer. Always Ollama unless explicitly overridden.

    Foundation Models was measured here and rejected as the default: asked "what
    is blocking the release?" against a transcript that says exactly that, it
    replied "No info in the loaded context". It also leaked an unrelated
    instruction ("I can't identify participants from audio") into a ticket
    question, and produced markdown headings despite being told not to. A ~3B
    system model is simply less reliable at multi-rule grounding, and a wrong
    answer mid-meeting costs more than the seconds it saves.

    Set LLM_BACKEND=foundation to prefer it anyway (lower power, ~2x faster).
    """
    if config.LIVE_BACKEND == "foundation" and backend() == "foundation":
        yield from _foundation_stream(system, prompt, max_tokens)
        return

    yield from ollama_client.chat_stream(
        config.LIVE_MODEL, system, prompt,
        num_ctx=config.LIVE_NUM_CTX, num_predict=max_tokens, temperature=temperature,
    )


def complete(
    system: str, prompt: str, *, max_tokens: int, temperature: float = 0.1,
    timeout: float = 240.0, task: str = "summary",
) -> str:
    """Non-streaming completion.

    `task` selects the backend: "live" answers must use the accurate model,
    "summary" uses the faster one. Routing live answers through the summary
    backend by default silently degraded them — the non-streaming answer path
    went to Foundation Models and started abstaining on answerable questions.
    """
    chosen = config.LIVE_BACKEND if task == "live" else config.SUMMARY_BACKEND
    if chosen == "foundation" and backend() == "foundation":
        return "".join(_foundation_stream(system, prompt, max_tokens))

    model = config.LIVE_MODEL if task == "live" else config.SUMMARY_MODEL
    num_ctx = config.LIVE_NUM_CTX if task == "live" else config.SUMMARY_NUM_CTX
    return ollama_client.chat(
        model, system, prompt,
        num_ctx=num_ctx, num_predict=max_tokens, temperature=temperature,
        timeout=timeout,
    )
