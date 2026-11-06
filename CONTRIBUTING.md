# Contributing to Clueless

Thanks for helping. This guide covers setup, the checks every change must pass,
and how changes become releases.

## Development setup

You need macOS 26 on Apple silicon, Node 22.12 or later, Python 3.11 or later,
and the Xcode Command Line Tools (`xcode-select --install`). Full Xcode is not
required.

```bash
# Native helpers: audio tap, OCR, Foundation Models bridge
cd native
./setup-signing.sh   # once: a self-signed identity so macOS remembers audio grants
./build.sh
cd ..

# Python API
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Electron overlay
cd overlay && npm ci
npm run start:ls     # launches through LaunchServices; see docs/permissions.md
```

Use `npm run start:ls`, not `npm start`. If Electron is started from a terminal,
macOS treats the terminal as the app asking for audio, and system audio
silently returns zeros. [docs/permissions.md](docs/permissions.md) explains why.

## Checks

Install the hooks once:

```bash
pip install pre-commit && pre-commit install
```

They run gitleaks, ruff, bandit, ESLint with security rules, a check that no
`.env` file is staged, and a denylist of organisation-specific terms.

To use the denylist, put one term per line in an untracked `.denylist` file at
the repository root. It is gitignored, because publishing the list would
publish the names it protects.

Tests that need no audio and no model run in CI on every pull request.
pytest collects them and enforces a coverage floor; each suite also still runs
on its own:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest                  # coverage floor included
python3 -m server.test_security    # API auth, origin and host checks
python3 -m server.test_phi         # identifier redaction, positive and negative cases
python3 -m server.test_profiles    # persona guarantees
python3 -m server.test_providers   # fallback ladder against fake local servers
python3 -m server.test_corrections # vocabulary corrections
python3 -m server.test_textclean
cd overlay && npm test && npm run lint
```

Tests that need a model or real audio run locally:

```bash
python3 -m server.test_brain          # answer quality; run once per profile:
PROFILE=medical python3 -m server.test_brain
python3 -m server.test_transcribe_e2e # system audio to transcript
python3 -m server.test_mic_e2e        # microphone to transcript
```

**If you change a prompt, run `test_brain` for every profile.** Shortening
prompts has cost answer quality before, in ways only the behavioural tests
caught. Examples inside the prompt get copied into answers verbatim by small
models, so describe the rule instead of giving a sample answer.

## Pull requests

`main` does not accept a direct push. Open a pull request. CI has to pass
before it can merge, and [PR Agent](https://github.com/The-PR-Agent/pr-agent)
reviews the diff. That review is sent to the model provider configured for the
repository, so a pull request must not contain a transcript, a token, or the
contents of `.env`.

- Keep each PR to one change, with a test where the behaviour can be tested.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for PR titles,
  because they decide the version bump: `feat:` is a minor release, `fix:` is a
  patch, and `feat!:` or a `BREAKING CHANGE:` footer is a major release. Use
  `docs:`, `chore:`, `refactor:` and `test:` for everything else.
- UI changes need a before and after screenshot. The overlay can render itself
  without screen-recording permission:
  ```bash
  cd overlay && ./node_modules/.bin/electron . --screenshot /tmp/card.png --state card
  ```
  Other flags: `--state pill|ask`, `--streaming`, `--toast`, `--degraded`,
  `--profile medical`, `--tour-step 1`, `--onboarding-step 3`, `--requirements`.
- Never weaken a security boundary without discussing it in an issue first.
  That includes the loopback-only provider check, the API token, the renderer
  sandbox, and medical-mode egress.

## Code style

- Python: `ruff check server/` (configured in `pyproject.toml`).
- JavaScript: `npm run lint` in `overlay/`. Build DOM with `textContent` and
  `createElement`. `innerHTML` is banned by lint, because transcripts and model
  output are untrusted.
- Comments explain constraints the code can't show, such as why something is
  needed and what failed without it. They don't narrate what the next line does.

## Releases

Merges to `main` update a release PR that `release-please` maintains. Merging
that PR tags `vX.Y.Z`, publishes the GitHub Release, and triggers the DMG build.
See [docs/building-and-releasing.md](docs/building-and-releasing.md).
