#!/usr/bin/env bash
# Builds a distributable Clueless.app.
#
# Refuses to run if anything credential-shaped would be swept into the bundle.
# That check exists because .env holds live API tokens and sits in the project
# root — a careless `zip -r .` or a packaging config that forgot to exclude it
# would publish them.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

fail() { echo "release blocked: $*" >&2; exit 1; }

# Build credentials live in .env.signing, never in .env: that file is the app's
# runtime environment and these values should not be visible to the running app.
if [ -f .env.signing ]; then
    echo "==> loading signing credentials"
    set -a; . ./.env.signing; set +a
fi

echo "==> pre-flight checks"

[ -f .env ] && fail ".env is present. Move it out of the tree (or delete it) before packaging — it contains API tokens."
[ -d .git ] || echo "    note: not a git repository; provenance cannot be verified"

# Anything that looks like a live token in files that would ship.
if grep -rIlE "ATATT[0-9A-Za-z=_-]{10,}|gho_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}" \
    --exclude-dir=node_modules --exclude-dir=.cache --exclude-dir=dist \
    --exclude-dir=.chroma --exclude-dir=build . 2>/dev/null | grep -q .; then
    fail "a live-looking token was found in the tree (see above)"
fi

for binary in native/build/CluelessTap.app native/build/ocr native/build/fmcli; do
    [ -e "$binary" ] || fail "missing $binary — run native/build.sh first"
done

command -v node >/dev/null || fail "node is required"
node -e 'const v=process.versions.node.split(".").map(Number); if (v[0]<22||(v[0]===22&&v[1]<12)) { console.error("node >= 22.12 required, found "+process.versions.node); process.exit(1) }'

echo "    all checks passed"

# The helper must carry the same identity as the app bundle, or notarization
# rejects the archive.
if [ -n "${CSC_NAME:-}" ]; then
    # CSC_NAME is the bare name electron-builder expects; codesign needs the
    # full identity string.
    export CLUELESS_SIGN_IDENTITY="Developer ID Application: ${CSC_NAME}"
    echo "    helper will be signed as: ${CLUELESS_SIGN_IDENTITY}"
else
    echo "    CSC_NAME not set — helper falls back to the self-signed dev cert,"
    echo "    which will fail notarization."
fi

echo "==> building native helpers"
(cd native && ./build.sh)

echo "==> installing overlay dependencies"
(cd overlay && npm install --silent)

echo "==> packaging"
cd overlay
if ! npx --no-install electron-builder --version >/dev/null 2>&1; then
    fail "electron-builder is not installed — run: cd overlay && npm install"
fi
if [ -z "${APPLE_ID:-}" ] || [ -z "${APPLE_APP_SPECIFIC_PASSWORD:-}" ] || [ -z "${APPLE_TEAM_ID:-}" ]; then
    echo
    echo "    NOTE: notarization credentials are incomplete (.env.signing)."
    echo "    The app will be signed but NOT notarized, so Gatekeeper will warn"
    echo "    on other Macs. See .env.signing.example."
    echo
fi
npx electron-builder --mac

echo
echo "==> done"
echo "    artifacts in overlay/dist/"
echo
echo "    Signing note: the native helper uses a self-signed certificate"
echo "    (\"Clueless Dev\"). A public build needs a Developer ID Application"
echo "    certificate and notarization, or Gatekeeper will block it on other Macs."
