# Changelog

All notable changes are recorded here. From the first public release this file
is maintained by [release-please](https://github.com/googleapis/release-please)
from Conventional Commit messages; edit entries in the release PR, not here.

## [0.2.1](https://github.com/jadenbanze/clueless/compare/v0.2.0...v0.2.1) (2026-11-06)


### Bug Fixes

* **ci:** pin RAM in provider test; skip CodeQL on private repos ([5c1793e](https://github.com/jadenbanze/clueless/commit/5c1793e3853e7f3cba46e61274ac20762b2dd6a5))
* **ci:** run the license check after npm ci ([2624d20](https://github.com/jadenbanze/clueless/commit/2624d20ea858b0b46add3bad2882f7cd78a9aca3))
* do not return model-server errors from the provider test ([ce78f11](https://github.com/jadenbanze/clueless/commit/ce78f1124dd9666dedb406a610aa2c7d3ae6a357))

## 0.2.0 (2026-11-06)

First public release.

### Features

- First-run walkthrough. It covers consent, choosing a profile, a system check,
  audio permissions with live level meters, model setup with a test answer, a
  test transcript, and a coach-mark tour of the overlay.
- Developer, Medical and General profiles. Each sets its own sources, prompt
  rules, summary format, privacy defaults and type scale.
- Medical profile: identifiers are redacted from everything shown, streamed,
  copied or exported, and network indexing is disabled.
- Pluggable local model providers: Ollama, OpenAI-compatible servers (llama.cpp,
  LM Studio, Jan), a managed `llama-server` for a GGUF file, and Apple
  Intelligence. The fallback ladder is shown in the overlay.
- Launch-time system check, plus a requirements window on unsupported Macs.
- Settings window with General, Model, Work sources, Advanced, Logs and About
  tabs.
- Shortcut cheat-sheet, health warning, an empty-state hint, and support for
  reduced motion.

### Security

- The local API requires a per-launch token. It rejects browser `Origin`s and
  non-loopback `Host` headers, which closes cross-site and DNS-rebinding access
  to live transcripts.
- Model provider URLs must be on loopback.
- Settings are validated against a strict schema. Request sizes are bounded.
  Errors return an opaque reference instead of internals.
- All renderers are sandboxed and cannot navigate or open windows.
- Logs record lengths and timings, never transcript text, and are written with
  owner-only permissions.
- Dependency upgrades clear all known advisories (`pip-audit`, `npm audit`).

### Fixes

- The on/off toast no longer overlaps the timer.
- Unchecking a setting whose default is on (such as "Listen only during calls")
  now takes effect.
- Starting capture twice no longer spawns duplicate audio helpers.
- Live answers no longer copy the prompt's example phrasing
  ("It is pending sign-off.") in place of naming the subject.
