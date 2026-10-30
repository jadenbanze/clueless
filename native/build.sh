#!/usr/bin/env bash
# Builds audiotap into a proper .app bundle.
#
# A bare CLI binary cannot hold the audio-capture TCC permission: with no bound
# Info.plist there is nothing for TCC to attribute the request to, so macOS
# silently hands the process tap buffers full of zeros instead of returning an
# error. Wrapping the binary in a signed bundle that declares
# NSAudioCaptureUsageDescription is what makes the permission grantable.
set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="CluelessTap"
BUNDLE="build/${APP_NAME}.app"
BUNDLE_ID="com.local.clueless.tap"

echo "==> compiling audiotap"
# Compile to a staging location first. Wiping the bundle before compiling means
# a failed build leaves a bundle with no executable, and LaunchServices then
# reports "its executable is missing" instead of the real compiler error.
mkdir -p build
STAGE="build/audiotap.stage"
swiftc -O main.swift speech.swift -o "${STAGE}" \
    -framework CoreAudio -framework AudioToolbox -framework AVFoundation -framework Speech -framework Foundation

rm -rf "${BUNDLE}"
mkdir -p "${BUNDLE}/Contents/MacOS"
mv "${STAGE}" "${BUNDLE}/Contents/MacOS/audiotap"

echo "==> compiling OCR helper"
swiftc -O -parse-as-library ocr.swift -o build/ocr \
    -framework Vision -framework Foundation -framework ImageIO

echo "==> compiling Foundation Models bridge"
swiftc -O -parse-as-library fmcli.swift -o build/fmcli \
    -framework FoundationModels -framework Foundation

echo "==> writing Info.plist"
cat >"${BUNDLE}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>audiotap</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.2</string>
    <!-- Background helper: no Dock icon, no menu bar. -->
    <key>LSUIElement</key>
    <true/>
    <!-- Required for the Core Audio process-tap API on macOS 14.4+. -->
    <key>NSAudioCaptureUsageDescription</key>
    <string>Clueless captures meeting audio locally to transcribe it on this machine.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Clueless captures your microphone locally to transcribe your own speech.</string>
</dict>
</plist>
PLIST

# Prefer a stable self-signed identity over ad-hoc. Ad-hoc signatures have no
# code identity, so TCC pins the audio-capture grant to the binary's CDHash and
# re-prompts on every source change. A real certificate keeps the designated
# requirement stable, so permission is granted once. See setup-signing.sh.
# Identity precedence:
#   1. CLUELESS_SIGN_IDENTITY — set by release.sh from .env.signing so the helper
#      matches the app. Notarization rejects a bundle whose nested binaries are
#      signed with a different identity.
#   2. The local self-signed certificate, for development.
SIGN_IDENTITY="${CLUELESS_SIGN_IDENTITY:-Clueless Dev}"
if security find-identity -v -p codesigning | grep -q "${SIGN_IDENTITY}"; then
    echo "==> signing with stable identity '${SIGN_IDENTITY}'"
    codesign --force --sign "${SIGN_IDENTITY}" \
        --identifier "${BUNDLE_ID}" \
        --options runtime \
        "${BUNDLE}"
else
    echo "==> WARNING: '${SIGN_IDENTITY}' not found; falling back to ad-hoc"
    echo "    (run ./setup-signing.sh to stop the repeated permission prompts)"
    codesign --force --sign - \
        --identifier "${BUNDLE_ID}" \
        --options runtime \
        "${BUNDLE}"
fi

echo "==> verifying"
codesign -dv "${BUNDLE}" 2>&1 | grep -E "Identifier|Info.plist|Signature|Authority"
echo "==> designated requirement (stable across rebuilds if identity-based):"
codesign -d -r- "${BUNDLE}" 2>&1 | grep -E "designated" || true

echo
echo "built: ${BUNDLE}"
echo "run:   ${BUNDLE}/Contents/MacOS/audiotap --list"
