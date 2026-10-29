# Clueless — one-time setup

A local-only meeting assistant for Teams, Webex, FaceTime, Zoom and Skype. It listens to your meeting,
transcribes it on-device, and answers questions on a hotkey using your Jira,
Confluence and code as context. Nothing is uploaded anywhere.

This guide takes about 15 minutes, once. After that, `npm start` is the only
command you need.

---

## 1. Hardware and software requirements

### Required

| | Minimum | Recommended | Why |
|---|---|---|---|
| **Chip** | Apple silicon (M1) | M2 Pro or better | The audio tap API and the on-device speech model are arm64 only |
| **RAM** | 16 GB | 24 GB+ | Measured footprint: ~350 MB for Clueless itself, plus ~5 GB when the 8B language model is loaded |
| **macOS** | **26.0 (Tahoe)** | 26.7+ | `SpeechAnalyzer`/`SpeechTranscriber` and Foundation Models only exist in macOS 26 |
| **Disk** | 8 GB free | 15 GB free | Ollama models (~5 GB), code index (~1 GB), four shallow repo clones |
| **Node** | 22.12+ | 22.23 LTS | Electron 44 requires ≥22.12; 22.11 fails on an ESM `require()` |
| **Python** | 3.11 | 3.11 | Tested on 3.11; 3.12+ untested |
| **Ollama** | installed | running | Local model server; also used for embeddings |

### Optional

| | Needed for | Notes |
|---|---|---|
| **Apple Intelligence enabled** | Faster summaries | Summaries run on the Neural Engine via Foundation Models. Without it, summaries fall back to Ollama automatically — slower, same quality |
| **Screen Recording permission** | Screen context (`⌘⇧4`) | OCR itself needs no permission; *capturing* the screen does |
| **Atlassian API token** | Jira + Confluence context | Without it those sources stay empty; answers use transcript only |
| **GitHub PAT** | Code context | Falls back to `gh auth token` |

### What does *not* work

- **Intel Macs.** The process-tap and on-device speech APIs used here are arm64.
- **macOS 15 or earlier.** No `SpeechTranscriber`, no Foundation Models.
- **Machines where the corporate network blocks `registry.ollama.ai`.** You
  cannot pull models. Existing models on disk still work; this is why the app
  pins specific models rather than downloading them.

### Measured footprint (M3 Pro, 36 GB)

| Component | Resident memory |
|---|---|
| Electron overlay | 172 MB |
| Python API | 130 MB |
| Audio helpers (×2) | 42 MB |
| Ollama idle | 51 MB (model unloaded) |
| Ollama with 8B loaded | ~5 GB |

---

## 2. Install

```bash
cd ~/Desktop/personal_projects/clueless
```

### 2.1 Build the native helpers

Creates a signed `.app` bundle for audio capture, plus OCR and language-model
bridges.

```bash
cd native
./setup-signing.sh     # creates a self-signed "Clueless Dev" certificate (once)
./build.sh
```

`setup-signing.sh` is idempotent — safe to re-run. Without a stable certificate,
macOS re-prompts for audio permission on *every* rebuild, because an ad-hoc
signature has no identity for TCC to remember.

Verify:

```bash
ls build/CluelessTap.app build/ocr build/fmcli
```

### 2.2 Install overlay dependencies

```bash
cd ../overlay && npm install
```

### 2.3 Add credentials

Copy the template and fill in what you have:

```bash
cd .. && cp .env.example .env && chmod 600 .env
```

```ini
ATLASSIAN_EMAIL=you@example.com
ATLASSIAN_API_TOKEN=...        # id.atlassian.com/manage-profile/security/api-tokens
GITHUB_TOKEN=...               # fine-grained PAT, read-only Contents on your org
SPEECH_LOCALE=en-US            # try en-GB / en-IN if accents transcribe poorly
```

`.env` is gitignored and `chmod 600`. Never commit it.

### 2.4 Build the index

One-off, and re-run whenever tickets or repos change materially.

```bash
python3 -c "from server import indexers; print(indexers.build_all())"
```

Expected output: ~800 Jira issues, ~500 Confluence chunks, ~4,300 code chunks.
Takes roughly 8 minutes; code indexing dominates.

### 2.5 Grant permissions

There are up to four, and they are granted differently:

| Permission | How | When |
|---|---|---|
| **System audio** | Prompt on first run | Approve **"CluelessTap would like to record your system audio"** |
| **Microphone** | Prompt on first run | Approve |
| **Screen Recording** | System Settings → Privacy & Security → Screen Recording | Only if you want `⌘⇧4` |
| **Accessibility** | Not needed | — |

Check the first two at any time:

```bash
cd native && ./check-permissions.sh
```

Expect both peaks non-zero:

```
PROBE [mic]    peak=0.07
PROBE [system] peak=0.83
```

> **Note:** changing the bundle identifier (`com.local.clueless.tap`) resets the
> audio grant. If you rebrand again, expect one fresh prompt.

---

### First run

Open **Settings…** from the menu bar. Leave developer mode off for general use,
or turn it on and fill in your Jira site, project key, Confluence spaces and
GitHub repos. Settings save to `.cache/settings.json`; credentials stay in `.env`.

If you entered organisation details, build the index once:

```bash
python3 -c "from server import indexers; print(indexers.build_all())"
```

Unconfigured sources are skipped cleanly — the app still runs as a general
assistant.

## 3. Run

```bash
cd overlay && npm start
```

To keep it running after closing the terminal:

```bash
nohup npm start >/dev/null 2>&1 &
```

A `◉` appears in the menu bar when capturing (`○` when idle). The overlay itself
stays hidden until you ask for it.

### Supported apps

Detection and capture follow any app listed in `TARGET_APPS` (`server/config.py`):

| App | Bundle ID substring | Notes |
|---|---|---|
| Microsoft Teams | `microsoft.teams2` | Verified working |
| Webex | `cisco-systems.spark` | Verified detected (idle) |
| FaceTime | `com.apple.facetime` + `avconferenced` | Call audio comes from `avconferenced`, not the app |
| Zoom | `zoom.us` | Bundle ID only; not installed on this machine |
| Skype | `skype` | Bundle ID only; Microsoft has retired Skype |

Matching is substring-based on the bundle ID because these apps emit audio from
helper processes rather than their main PID — Teams alone spawns five. Adding an
app is one line; no rebuild.

Check what is detected at any time:

```bash
native/build/CluelessTap.app/Contents/MacOS/audiotap --detect \
  --bundle "$(python3 -c 'from server import config; print(config.TARGET_APP_MATCH)')"
# active=0 matched=6 processes=      (active=1 means a call is running)
```

### Hotkeys

| Key | Action |
|---|---|
| `⌘⇧\` | Ask about whatever was just said |
| `⌘⇧S` | Summarise the meeting so far |
| `⌘⇧H` | Show / hide the overlay |
| `⌘⇧4` | Toggle **screen** context (captures on enable) |

Developer mode adds three more:

| Key | Action |
|---|---|
| `⌘⇧1` | Toggle **Jira** context |
| `⌘⇧2` | Toggle **Confluence** context |
| `⌘⇧3` | Toggle **code** context |

Everything is also available by clicking the menu bar icon, including
**Save summary…** — the only artefact that outlives a meeting.

### General use vs developer mode

**Out of the box this is a general meeting assistant.** It transcribes, answers
questions from the conversation, and can read your screen. No Jira, Confluence
or code is consulted, and those controls do not appear at all.

To enable the work-specific sources, set this in `.env` and restart:

```ini
DEVELOPER_MODE=1
```

That unlocks `⌘⇧1` Jira, `⌘⇧2` Confluence and `⌘⇧3` code, and defaults Jira and
Confluence to on. Without it, those endpoints return `403` and their hotkeys are
never bound — so there is no way to end up with a half-enabled state.

The reason for the gate is not just tidiness. In a meeting unrelated to your
codebase, retrieved tickets and source files do not merely add noise — the model
builds confident answers out of them.

### Which sources to use (developer mode)

Defaults: **Jira + Confluence on, code + screen off.**

Code is off deliberately. In a meeting that is not about the codebase, retrieved
source files do not just add noise — the model tries to use them and produces
confident answers assembled from unrelated code. Turn it on (`⌘⇧3`) when the
conversation turns to implementation.

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No transcript at all | Audio permission missing | `./native/check-permissions.sh`; both peaks must be non-zero |
| One stream silent (`peak=0.0`) | That grant missing | Approve the prompt, or reset: `tccutil reset AudioCapture com.local.clueless.tap` |
| Prompt appears on every rebuild | Ad-hoc signature | Run `native/setup-signing.sh` |
| Hotkeys do nothing | App not running | Check for `◉` in the menu bar |
| `EADDRINUSE` on 8766 | Another copy is running | `pkill -f "server.api"` |
| Answers say "No info in the loaded context" | Nothing indexed, or wrong sources enabled | Re-run indexing; check the footer chips |
| `⌘⇧1`–`⌘⇧3` do nothing | Developer mode is off | Set `DEVELOPER_MODE=1` in `.env` and restart |
| Screen context says it cannot read the screen | Screen Recording not granted | System Settings → Privacy & Security → Screen Recording |
| `ollama pull` fails with "invalid character '<'" | Corporate network blocks the registry | Use models already on disk; do not try to bypass |
| Answers take 8s+ | Model cold-loading | First question after idle pays ~2s; `OLLAMA_KEEP_ALIVE` controls this |

Logs:

```bash
tail -f /tmp/clueless_mic_helper.log       # microphone helper
tail -f /tmp/clueless_system_helper.log    # system-audio helper
```

---

## 5. Privacy

Verified, not just claimed:

- **No network egress** during operation. `lsof` on the API, Electron, audio
  helpers and Ollama shows only loopback connections.
- Outbound requests happen **only during indexing** — to Atlassian and GitHub,
  both explicit actions you trigger.
- Chroma telemetry is disabled (`anonymized_telemetry=False`).
- **Transcripts are never written to disk.** They live in RAM and are gone when
  the app exits. A summary exists only if you press `⌘⇧S`, and is only saved if
  you choose **Save summary…**.
- Credentials live in `.env` (mode 0600, gitignored) and are passed to `git` via
  the environment, never in process arguments.

### Before using it on a call with other people

Recording participants has consent implications that vary by jurisdiction, and
your employer may have an acceptable-use policy covering this class of tool.
Confirm you are cleared before pointing it at a real meeting.

---

## 6. Updating

```bash
git pull
cd native && ./build.sh        # only if Swift sources changed
cd ../overlay && npm install   # only if package.json changed
python3 -c "from server import indexers; print(indexers.build_all())"   # refresh context
```

---

## 7. Uninstall

```bash
pkill -f "server.api"; pkill -f "overlay/node_modules/electron"; pkill -x audiotap
security delete-certificate -c "Clueless Dev" ~/Library/Keychains/login.keychain-db
tccutil reset AudioCapture com.local.clueless.tap
rm -rf .chroma .cache native/build overlay/node_modules
```

Your Jira and GitHub tokens should also be revoked at the provider.
