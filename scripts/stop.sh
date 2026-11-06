#!/usr/bin/env bash
# Stop Clueless processes (API, Electron overlay, audio helpers).
#
# Order matters: the API goes first so its audio helpers stop cleanly, then
# Electron gets SIGTERM and time to quit gracefully. Anything still alive after
# the grace period is SIGKILLed. Use pkill -x audiotap (exact name) — never
# pkill -f audiotap, which matches this shell's own command line.
set -uo pipefail

GRACE_SECONDS=8

# Patterns that identify Clueless processes. Each must be specific enough not
# to match this script or unrelated apps (Cursor, Postman, acreom).
PATTERNS=(
    "server.api"
    "overlay/node_modules/electron"
    "Clueless.app/Contents/MacOS"
    "CluelessTap"
)

alive() {
    local p
    for p in "${PATTERNS[@]}"; do
        pgrep -f "$p" >/dev/null 2>&1 && return 0
    done
    pgrep -x audiotap >/dev/null 2>&1 && return 0
    return 1
}

list_alive() {
    local p
    for p in "${PATTERNS[@]}"; do
        pgrep -lf "$p" 2>/dev/null
    done
    pgrep -lx audiotap 2>/dev/null
}

signal_all() {
    local sig="$1" p
    for p in "${PATTERNS[@]}"; do
        pkill "-$sig" -f "$p" 2>/dev/null || true
    done
    pkill "-$sig" -x audiotap 2>/dev/null || true
}

echo "==> stopping Clueless"

if ! alive; then
    echo "  nothing running"
    exit 0
fi

# 1. Graceful: SIGTERM, then wait for Electron and the API to finish quitting.
signal_all TERM
for ((i = 0; i < GRACE_SECONDS * 4; i++)); do
    alive || break
    sleep 0.25
done

# 2. Force: anything still alive after the grace period.
if alive; then
    echo "  still running after ${GRACE_SECONDS}s, forcing"
    signal_all KILL
    sleep 0.5
fi

echo "==> remaining (should be empty)"
remaining="$(list_alive | sort -u)"
if [ -z "$remaining" ]; then
    echo "  (none)"
else
    echo "$remaining"
    echo "  warning: some processes survived SIGKILL"
    exit 1
fi
