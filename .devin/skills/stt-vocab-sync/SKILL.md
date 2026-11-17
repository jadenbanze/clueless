---
name: stt-vocab-sync
description: Audit on-device transcription health, noise-gate telemetry, echo suppression, and domain vocabulary corrections
allowed-tools:
  - read
  - grep
  - glob
  - exec
---

Inspect and tune the Clueless local transcription pipeline (`native/speech.swift` + `server/socket_server.py` + `server/harvest_corrections.py`):

1. Run deterministic speech cleanup and vocabulary regression suites:
   - `python3 -m server.test_textclean`
   - `python3 -m server.test_corrections`
2. Preview auto-harvested vocabulary from the local index without network calls:
   - `python3 -m server.harvest_corrections`
3. Check helper diagnostics in `logs/mic-helper.log` and `logs/system-helper.log` (if present):
   - Confirm `buffers=` and `gated=` counts advance and `floor=` stays reasonable (~0.001–0.01)
   - Ensure common English words are never mapped as single-word overrides (use word boundaries and phrase-level bigrams or multi-syllable proper-noun matching)
