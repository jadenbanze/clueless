# Privacy

Clueless is built so that meeting content never leaves your Mac. This page
lists what is stored, what makes network requests, and how to check those
claims yourself.

## What is stored

| Data | Where | Lifetime |
|---|---|---|
| Audio | Memory inside the helper, a few seconds at a time | Discarded once transcribed |
| Transcript | Memory inside the API, as a rolling window of recent turns | Lost on quit. Never written to disk |
| Answers | Memory | Same as the transcript |
| Summary | Only where you export it | You choose |
| Search index (Jira, Confluence, code) | `.chroma/vectors.sqlite3` in the data folder | Until you delete it |
| Settings | `.cache/settings.json` | Until you delete it |
| Credentials | `.env` (mode 600) | Until you delete it |
| Logs | `logs/` (folder mode 700) | Rotated at 1 MB, 5 kept. They hold timings, lengths and errors, never transcript text |

The data folder is `~/Library/Application Support/Clueless` for the installed
app, and the project root when running from source.

## What uses the network

Only actions you start yourself, and only in the Developer and General profiles:

| Action | Destination |
|---|---|
| Indexing Jira or Confluence | Your Atlassian site |
| Indexing repositories | `github.com` (a `git clone`, with the token passed through `GIT_ASKPASS`, never on the command line) |
| First use of a speech language | Apple's asset servers, through the Speech framework |

Live use (listening, transcribing, answering, summarising) makes **no**
external connections. Model providers must be on loopback, and every model
request goes through one helper (`server/providers/http.py`) that refuses any
other address before connecting. The Medical profile disables indexing in
code, so it makes no network requests of its own at all.

The search index is a local SQLite file searched with numpy. It contains no
vector database service and no telemetry.

FastAPI depends on `opentelemetry-api`. Only that interface package ships, with
no SDK or exporter, so it records nothing and has nowhere to send anything. The
app also sets `OTEL_SDK_DISABLED=true`.

## The local API

The overlay talks to a Python API on `127.0.0.1:8766`. A web page in your
browser can also reach that port, so the API:

- requires a random token generated at each launch, which the browser never
  sees;
- rejects requests carrying a browser `Origin`;
- rejects `Host` headers that aren't loopback, which defeats DNS rebinding;
- checks the token on WebSocket connections as well.

The only route that needs no token is `/livez`, which returns `ok` and nothing
else. [SECURITY.md](../SECURITY.md) has the full threat model.

## Checking it yourself

During a live meeting:

```bash
for p in $(pgrep -f "server.api|Clueless|audiotap|ollama"); do
  lsof -nP -a -p "$p" -i TCP 2>/dev/null
done | grep -v -E "127\.0\.0\.1|\[::1\]|localhost" | grep ESTABLISHED
```

No output means no external connections. IPv6 `fe80::` link-local entries are
mDNS, not internet traffic.

To find every outbound URL in the source:

```bash
rg -n "https?://" server/ overlay/*.js native/*.swift | rg -v "127\.0\.0\.1|localhost"
```

## Consent

Running locally protects the data. It doesn't make recording lawful. Many places
require every participant's consent before a conversation is recorded or
transcribed, and many employers have their own policies. Tell people you're
using a transcription assistant. The walkthrough asks you to confirm this
before it continues.
