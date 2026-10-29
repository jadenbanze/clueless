# Clueless

> **New here? Start with [SETUP.md](SETUP.md)** — hardware requirements, one-time setup, permissions and troubleshooting.

A local-only meeting assistant for Teams, Webex, FaceTime, Zoom and Skype — for any industry, not just software. Captures meeting audio,
transcribes it on-device, and answers questions on a hotkey using your Jira
tickets and repository code as context. Nothing leaves the machine.

## Requirements

- macOS 26+ on Apple silicon (uses the macOS 26 on-device Speech API)
- Node ≥ 22.12 and Python 3.11
- Ollama running locally with `llama3.1:8b` and `nomic-embed-text`

## First-time setup

```bash
# 1. Build the audio helper (creates a signed .app bundle)
cd native && ./setup-signing.sh && ./build.sh

# 2. Verify both audio permissions resolve
./check-permissions.sh          # run from VS Code/Cursor for both streams

# 3. Install overlay dependencies
cd ../overlay && npm install

# 4. Add Jira credentials to .env (see below), then index context
cd ..
python3 -c "from server import indexers; print(indexers.build_all())"
```

### Jira credentials

Put them in `.env` at the project root — not in your shell. The API is spawned by
Electron as a child process, so exported variables never reach it.

```
ATLASSIAN_EMAIL=you@example.com
ATLASSIAN_API_TOKEN=...
```

Create a token at <https://id.atlassian.com/manage-profile/security/api-tokens>.
Use an API token, not your password, give it a short expiry, and revoke it when
you stop using this. `.env` is gitignored; `.env.example` documents the keys.

Verify it was picked up (values are never reported, only presence):

```bash
curl -s http://127.0.0.1:8766/health | python3 -m json.tool
```

```json
"jira_credentials": { "email_set": true, "token_set": true }
```

## Run

```bash
cd overlay && npm start
```

| Hotkey | Action |
|---|---|
| `⌘⇧\` | Answer the current question (shows the overlay) |
| `⌘⇧H` | Show/hide the overlay |
| `⌘⇧S` | Summarise the meeting so far |

The overlay is excluded from screen capture, so it does not appear to anyone you
share your screen with.

## Configure

Everything is set up from **Settings…** in the menu bar, or in `.env`:

| Setting | Purpose |
|---|---|
| Developer mode | Adds Jira / Confluence / code sources |
| Jira site + project | Ticket context (developer mode) |
| Confluence spaces | Wiki context (developer mode) |
| GitHub org + repos | Code context (developer mode) |
| Recognition locale | Pick a variant that suits the accents in your meetings |
| Custom vocabulary | `heard=correct` pairs — teach it names, drug names, acronyms |

Credentials (API tokens) live only in `.env`, never in the settings window.

## Pause

The menu bar icon has **Pause listening** / **Resume listening**. Pausing stops
audio capture immediately without quitting — use it when a conversation is not
something the assistant should hear. The icon shows `⏸` while paused.

## Tests

```bash
python3 -m server.test_corrections    # fast, offline
python3 -m server.test_brain          # answers + summary (needs Ollama)
python3 -m server.test_transcribe_e2e # system audio -> transcript
python3 -m server.test_mic_e2e        # microphone -> transcript
```

## Before using this on a real call

Recording meeting participants has consent implications that vary by
jurisdiction, and your employer may have an acceptable-use policy covering this
class of tool. This machine is managed, and its network blocks the "Generative
AI and ML Applications" category — confirm you are cleared to run this before
pointing it at a call with other people on it.

Transcripts are held in memory only and are never written to disk. A summary
exists only if you request one, and is not saved either.

## Layout

```
native/    Swift helper: Core Audio process tap + on-device transcription
server/    Python: socket ingest, retrieval, answers, HTTP/WebSocket API
overlay/   Electron: screen-share-invisible panel, hotkeys
```

See `AGENTS.md` for the hard-won details: TCC behaviour, why the microphone and
system audio need different launch paths, and the measurement results behind the
model and transcriber choices.
