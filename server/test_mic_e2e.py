"""End-to-end test for the microphone path.

Unlike the system-audio test, the helper is spawned directly (not via
LaunchServices) because the microphone grant is inherited from the parent
process — Electron in the app, the terminal in this test.

Run: python3 -m server.test_mic_e2e
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time

from . import config
from .socket_server import TranscriptSocketServer, TranscriptStore

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("mic-e2e")

PHRASE = "The microphone capture path is working correctly today."
EXPECTED = ["microphone", "capture", "working"]

SOCKET_PATH = os.path.join(tempfile.gettempdir(), "mc_mic.sock")


def main() -> int:
    if not config.TAP_BINARY.exists():
        log.error("helper not built: run native/build.sh")
        return 1

    audio_path = os.path.join(tempfile.gettempdir(), "mc_mic_phrase.aiff")
    subprocess.run(["say", "-o", audio_path, " ".join([PHRASE] * 4)], check=True)
    duration = float(
        subprocess.run(["afinfo", audio_path], capture_output=True, text=True)
        .stdout.split("estimated duration: ")[1].split(" sec")[0]
    )

    store = TranscriptStore()
    server = TranscriptSocketServer(SOCKET_PATH, store)
    server.start()

    log_path = os.path.join(tempfile.gettempdir(), "mc_mic_helper.log")
    if os.path.exists(log_path):
        os.unlink(log_path)

    player = subprocess.Popen(["afplay", audio_path], stdout=subprocess.DEVNULL)
    time.sleep(1.0)

    # Direct spawn: this is what makes the mic grant inheritable.
    helper = subprocess.Popen(
        [
            str(config.TAP_BINARY), "--transcribe", "--source", "mic",
            "--socket", SOCKET_PATH, "--log", log_path,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    log.info("capturing for %.0fs", duration + 4)
    deadline = time.time() + duration + 4
    while time.time() < deadline:
        time.sleep(1)

    player.terminate()
    helper.terminate()
    try:
        helper.wait(timeout=5)
    except subprocess.TimeoutExpired:
        helper.kill()
    time.sleep(1)
    server.stop()

    turns = store.snapshot()
    print("\n===== TRANSCRIPT =====")
    for turn in turns:
        print(f"  [{turn.speaker}] {turn.text}")
    if not turns:
        print("  (empty)")

    print("\n===== HELPER LOG =====")
    if os.path.exists(log_path):
        with open(log_path) as handle:
            print(handle.read().strip() or "  (empty)")
        os.unlink(log_path)

    combined = store.full_text().lower()
    matched = [word for word in EXPECTED if word in combined]
    print("\n===== RESULT =====")
    print(f"turns captured: {len(turns)}")
    print(f"expected words matched: {matched}/{EXPECTED}")
    print(f"speaker labels: {sorted({t.speaker for t in turns})}")

    if len(matched) >= 2 and all(t.speaker == "me" for t in turns):
        print("PASS — microphone transcription works and is labelled 'me'")
        return 0
    print("FAIL — see transcript above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
