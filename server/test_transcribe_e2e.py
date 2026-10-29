"""End-to-end test: real audio -> Swift tap -> on-device STT -> socket -> store.

Plays a known phrase through the speakers, launches the system-audio helper the
same way the app does (LaunchServices, so it uses its own bundle identity), and
asserts that the expected words appear in the transcript.

Run: python3 -m server.test_transcribe_e2e
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import config
from .socket_server import TranscriptSocketServer, TranscriptStore

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("e2e")

# Words chosen to be distinctive so a partial transcription still matches.
PHRASE = "The Kafka authentication failure is blocking the restaurant profile release."
EXPECTED = ["kafka", "authentication", "restaurant", "profile"]

SOCKET_PATH = os.path.join(tempfile.gettempdir(), "mc_e2e.sock")


def play_audio(path: str) -> subprocess.Popen:
    return subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def main() -> int:
    if not config.TAP_APP.exists():
        log.error("helper not built: run native/build.sh")
        return 1

    audio_path = os.path.join(tempfile.gettempdir(), "mc_e2e_phrase.aiff")
    # Repeat the phrase so audio is definitely playing throughout the capture
    # window: helper startup (tap creation + speech-asset check + engine init)
    # takes long enough that a single 4s utterance can end before capture begins.
    subprocess.run(["say", "-o", audio_path, " ".join([PHRASE] * 5)], check=True)
    duration = float(
        subprocess.run(["afinfo", audio_path], capture_output=True, text=True)
        .stdout.split("estimated duration: ")[1].split(" sec")[0]
    )
    log.info("test audio: %.1fs", duration)

    store = TranscriptStore()
    server = TranscriptSocketServer(SOCKET_PATH, store)
    server.start()

    log_path = os.path.join(tempfile.gettempdir(), "mc_e2e_helper.log")
    if os.path.exists(log_path):
        os.unlink(log_path)

    player = play_audio(audio_path)
    time.sleep(1.5)

    # LaunchServices launch: required for the AudioCapture grant to apply.
    subprocess.run(
        [
            "open", "-n", "-a", str(config.TAP_APP), "--args",
            "--transcribe", "--source", "system", "--global",
            "--socket", SOCKET_PATH, "--log", log_path,
        ],
        check=True,
    )

    log.info("helper launched; capturing for %.0fs", duration + 6)
    deadline = time.time() + duration + 6
    while time.time() < deadline:
        time.sleep(1)

    player.terminate()
    time.sleep(2)
    subprocess.run(["pkill", "-f", "CluelessTap"], capture_output=True)

    turns = store.snapshot()
    server.stop()

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

    shutil.rmtree(audio_path, ignore_errors=True)

    if len(matched) >= 2:
        print("PASS — on-device transcription reached the socket")
        return 0
    print("FAIL — expected words were not transcribed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
