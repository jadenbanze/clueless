#!/usr/bin/env bash
# Launch the Electron overlay via LaunchServices so TCC attributes mic /
# audio grants to Electron.app, not the terminal emulator.
#
# Plain `npm start` / `electron .` from Warp makes the terminal the
# responsible process — mic may work (Warp declares it) while system audio
# silently fails. See AGENTS.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY="$ROOT/overlay"
ELECTRON_APP="$OVERLAY/node_modules/electron/dist/Electron.app"

[ -d "$ELECTRON_APP" ] || {
    echo "Electron.app missing — run: cd overlay && npm install" >&2
    exit 1
}

echo "starting via LaunchServices: $ELECTRON_APP"
echo "  (overlay cwd: $OVERLAY)"
open -n -a "$ELECTRON_APP" --args "$OVERLAY"
