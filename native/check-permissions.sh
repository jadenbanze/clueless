#!/usr/bin/env bash
# Verifies that BOTH audio permissions resolve for the helper.
#
# TCC attributes a request to the "responsible process" — the app that owns the
# session the helper was launched from — not to the helper's own bundle. So the
# parent process determines which grants the helper inherits:
#
#   Warp.app        declares NSMicrophoneUsageDescription only  -> mic, no system audio
#   VS Code/Cursor  declare BOTH mic + NSAudioCaptureUsageDescription -> both
#
# Run this from the integrated terminal of VS Code, Cursor, Windsurf or Devin.app
# to get both streams. No admin rights are required: TCC grants are per-user.
set -uo pipefail

cd "$(dirname "$0")"

BINARY="build/CluelessTap.app/Contents/MacOS/audiotap"
if [ ! -x "${BINARY}" ]; then
    echo "helper not built — run ./build.sh first"
    exit 1
fi

# Identify the owning application, since that is what TCC will prompt for.
parent_app() {
    local pid=$PPID
    while [ "${pid}" -gt 1 ]; do
        local comm
        comm=$(ps -p "${pid}" -o comm= 2>/dev/null)
        case "${comm}" in
            *.app/Contents/MacOS/*) basename "$(echo "${comm}" | sed 's|/Contents/MacOS/.*||')"; return ;;
        esac
        pid=$(ps -p "${pid}" -o ppid= 2>/dev/null | tr -d ' ')
        [ -z "${pid}" ] && break
    done
    echo "unknown"
}

OWNER=$(parent_app)
echo "responsible process: ${OWNER}"

# Full ancestry, so it is obvious which app actually launched this shell.
echo "process ancestry:"
anc_pid=$PPID
while [ "${anc_pid}" -gt 1 ]; do
    printf '  %s\n' "$(ps -p "${anc_pid}" -o comm= 2>/dev/null | cut -c1-90)"
    anc_pid=$(ps -p "${anc_pid}" -o ppid= 2>/dev/null | tr -d ' ')
    [ -z "${anc_pid}" ] && break
done

case "${OWNER}" in
    Warp.app|Terminal.app|iTerm.app|Alacritty.app|kitty.app|Ghostty.app)
        echo
        echo "NOTE: ${OWNER} declares microphone but NOT NSAudioCaptureUsageDescription,"
        echo "      so system audio cannot be granted here. Expect [system] peak=0.0."
        echo "      Re-run from the integrated terminal of VS Code, Cursor or Windsurf."
        ;;
esac
echo

echo "==> playing test audio so both streams have signal"
say -o /tmp/mc_probe.aiff \
    "Testing microphone and system audio capture together, one two three four five." 2>/dev/null
afplay /tmp/mc_probe.aiff >/dev/null 2>&1 &
AFPLAY_PID=$!
trap 'kill ${AFPLAY_PID} 2>/dev/null; rm -f /tmp/mc_probe.aiff' EXIT
sleep 1

echo "==> probing (approve any permission prompts that appear)"
echo
"${BINARY}" --global --mic --probe 7 2>&1 | grep -E "microphone|layout|source mapping|PROBE|RESULT|WARNING"
echo

echo "Interpretation:"
echo "  PROBE [mic]    peak > 0  -> microphone capture works"
echo "  PROBE [system] peak > 0  -> system audio capture works"
echo "  peak = 0.0               -> permission missing for that stream"
