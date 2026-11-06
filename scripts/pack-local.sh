#!/usr/bin/env bash
# Build a double-clickable Clueless.app for THIS Mac only.
#
# No Apple Developer ID and no notarization. Uses the self-signed
# "Clueless Dev" identity when present, otherwise leaves the Electron
# app unsigned (Gatekeeper: System Settings > Privacy & Security > Open Anyway, or xattr -dr quarantine).
#
# For a notarized public build use scripts/release.sh + .env.signing.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

fail() { echo "pack-local blocked: $*" >&2; exit 1; }

echo "==> local pack (no Apple Developer / no notarization)"

command -v node >/dev/null || fail "node is required"
node -e 'const v=process.versions.node.split(".").map(Number); if (v[0]<22||(v[0]===22&&v[1]<12)) { console.error("node >= 22.12 required, found "+process.versions.node); process.exit(1) }'

if ! security find-identity -v -p codesigning 2>/dev/null | grep -q 'Clueless Dev'; then
    echo "==> creating self-signed Clueless Dev certificate"
    (cd native && ./setup-signing.sh)
fi

echo "==> building native helpers"
(cd native && ./build.sh)

for binary in native/build/CluelessTap.app native/build/ocr native/build/fmcli; do
    [ -e "$binary" ] || fail "missing $binary after native/build.sh"
done

if [ ! -x native/build/python/bin/python3 ]; then
    fail "missing relocatable Python at native/build/python — required for packaged runs"
fi

echo "==> installing overlay dependencies"
(cd overlay && npm install --silent)

# Prefer the login-keychain self-signed identity so TCC grants survive rebuilds
# for the helper. Electron itself is signed the same way when possible.
export CSC_IDENTITY_AUTO_DISCOVERY=false
IDENTITY="Clueless Dev"
if ! security find-identity -v -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
    IDENTITY="-"
    echo "    warning: Clueless Dev not found — ad-hoc signing the app bundle"
fi

echo "==> clearing previous dist (if any)"
# Previous packs can leave quarantine/immutable bits that block electron-builder
# from emptying the output dir ("operation not permitted" on nested python files).
DIST_DIR="$ROOT/overlay/dist/mac-arm64"
if [ -d "$DIST_DIR" ]; then
    chflags -R nouchg,noschg "$DIST_DIR" 2>/dev/null || true
    xattr -cr "$DIST_DIR" 2>/dev/null || true
    chmod -R u+w "$DIST_DIR" 2>/dev/null || true
    rm -rf "$DIST_DIR"
fi

echo "==> packaging (identity=$IDENTITY, notarize=false)"
cd overlay
if [ "$IDENTITY" = "-" ]; then
    # electron-builder 26 silently skips signing for identity "-"; the
    # afterPack hook does the ad-hoc signing instead.
    CLUELESS_ADHOC_SIGN=1 npx electron-builder --mac --dir \
        -c.mac.notarize=false \
        -c.mac.target=dir
else
    npx electron-builder --mac --dir \
        -c.mac.identity="$IDENTITY" \
        -c.mac.notarize=false \
        -c.mac.hardenedRuntime=false \
        -c.mac.target=dir
fi

BUNDLE="dist/mac-arm64/Clueless.app"
[ -d "$BUNDLE" ] || BUNDLE="dist/mac/Clueless.app"
[ -d "$BUNDLE" ] || fail "Clueless.app not found under overlay/dist/"

echo "==> verifying the bundle contains no credentials"
for leaked in ".env" ".env.signing" "settings.json"; do
    if find "$BUNDLE" -name "$leaked" -print -quit 2>/dev/null | grep -q .; then
        fail "$leaked was found inside the app bundle"
    fi
done
# Leftover helpers must not bloat the pack.
if find "$BUNDLE" -name 'MeetingCopilotTap.app' -print -quit 2>/dev/null | grep -q .; then
    fail "MeetingCopilotTap.app was bundled — tighten extraResources filters"
fi
if find "$BUNDLE" -name 'clueless-server' -print -quit 2>/dev/null | grep -q .; then
    fail "clueless-server was bundled — tighten extraResources filters"
fi
echo "    clean"

DATA_DIR="${HOME}/Library/Application Support/Clueless"
mkdir -p "$DATA_DIR"
if [ -f "$ROOT/.env" ] && [ ! -f "$DATA_DIR/.env" ]; then
    cp "$ROOT/.env" "$DATA_DIR/.env"
    chmod 600 "$DATA_DIR/.env"
    echo "==> seeded $DATA_DIR/.env from repo .env"
fi

ABS_BUNDLE="$(cd "$(dirname "$BUNDLE")" && pwd)/$(basename "$BUNDLE")"

echo
echo "==> done"
echo "    app:  $ABS_BUNDLE"
echo "    data: $DATA_DIR"
echo
echo "    First launch (Gatekeeper, unsigned/self-signed):"
echo "      open \"$ABS_BUNDLE\""
echo "      # or: System Settings → Privacy & Security → Open Anyway"
echo "      # or: xattr -dr com.apple.quarantine \"$ABS_BUNDLE\""
echo
echo "    Install (optional):"
echo "      cp -R \"$ABS_BUNDLE\" /Applications/"
echo "      # Prefer /Applications — Desktop/Documents/Downloads trigger TCC folder prompts."
