# Getting started from source

Most people should install the DMG from the
[latest release](../../../releases/latest). This page is for running Clueless
from a clone, to develop it or to try unreleased changes. It takes about 15
minutes the first time.

## Requirements

| | Minimum | Recommended | Why |
|---|---|---|---|
| Chip | Apple silicon (M1) | M2 Pro or later | The audio tap and on-device speech APIs are arm64 only |
| Memory | 8 GB (3B model) | 16 GB+ (8B model) | Clueless itself uses about 350 MB; an 8B model adds about 5 GB |
| macOS | 26.0 | latest 26.x | `SpeechAnalyzer` and Foundation Models first shipped in macOS 26 |
| Disk | 2 GB free | 10 GB free | Models are 2–5 GB each; the code index can reach 1 GB |
| Node | 22.12 | 22 LTS | Electron 44 requires it |
| Python | 3.11 | 3.12 | The packaged app bundles 3.12 |
| Tools | Xcode Command Line Tools | | `swiftc` builds the helpers; full Xcode is not needed |

Check a machine at any time with `cd overlay && npm run preflight`.

## 1. Build the native helpers

```bash
cd native
./setup-signing.sh   # once
./build.sh
```

This produces three binaries in `native/build/`:

- `CluelessTap.app`: audio capture and on-device transcription
- `ocr`: reads text off the screen with Apple Vision
- `fmcli`: a bridge to Apple's on-device language model

`setup-signing.sh` creates a self-signed code-signing identity in your login
keychain. Without it, every rebuild changes the helper's signature, and macOS
asks for audio permission again each time.
[permissions.md](permissions.md) explains why.

## 2. Install dependencies

```bash
cd ..
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd overlay && npm ci
```

## 3. Get a model

Any one of these works ([providers-and-models.md](providers-and-models.md) has
the details):

```bash
brew install ollama && ollama serve &
ollama pull llama3.1:8b          # or llama3.2 on 8 GB Macs
ollama pull nomic-embed-text     # only needed for Jira/Confluence/code context
```

## 4. Run

```bash
npm run start:ls
```

This launches Electron through LaunchServices. On first run, the walkthrough
opens and asks for microphone and system-audio access. To stop everything:

```bash
npm run stop
```

## 5. Optional: work sources (Developer profile)

Choose the Developer profile, then fill in **Settings → Work sources**.
Credentials go in `.env` at the project root, which is gitignored. For the
packaged app, `.env` lives in `~/Library/Application Support/Clueless/`.

```bash
cp .env.example .env && chmod 600 .env
```

Index once, and again whenever tickets or code change a lot:

```bash
python3 -c "from server import indexers; print(indexers.build_all())"
```

Unconfigured sources are skipped. The Medical profile skips indexing entirely.

## Supported call apps

Capture follows every app listed in `TARGET_APPS` in `server/config.py`.
Matching is by bundle-ID substring, because call apps play audio from helper
processes rather than their main process. Adding an app is one line, with no
rebuild.

| App | Bundle ID substring |
|---|---|
| Microsoft Teams | `microsoft.teams2` |
| Webex | `cisco-systems.spark` |
| FaceTime | `com.apple.facetime` and `avconferenced` (which actually plays FaceTime call audio) |
| Zoom | `zoom.us` |
| Skype | `skype` |

To see what is detected right now:

```bash
native/build/CluelessTap.app/Contents/MacOS/audiotap --detect \
  --bundle "$(python3 -c 'from server import config; print(config.TARGET_APP_MATCH)')"
# active=1 means a call is playing audio
```

## Where things live

| | Development | Packaged app |
|---|---|---|
| Settings | `.cache/settings.json` | `~/Library/Application Support/Clueless/.cache/settings.json` |
| Credentials | `.env` | `~/Library/Application Support/Clueless/.env` |
| Logs | `logs/` | `~/Library/Application Support/Clueless/logs/` |
| Search index | `.chroma/vectors.sqlite3` | `~/Library/Application Support/Clueless/.chroma/vectors.sqlite3` |

Transcripts are never written anywhere.

## Updating

```bash
git pull
cd native && ./build.sh && cd ..           # if Swift sources changed
pip install -r requirements.txt            # if requirements changed
cd overlay && npm ci                       # if package-lock changed
```

## Uninstalling

```bash
cd overlay && npm run stop
security delete-certificate -c "Clueless Dev" ~/Library/Keychains/login.keychain-db
tccutil reset AudioCapture com.local.clueless.tap
rm -rf ~/Library/Application\ Support/Clueless
```

Revoke any Atlassian or GitHub tokens you created for it.
