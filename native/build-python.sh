#!/usr/bin/env bash
# Builds the relocatable Python runtime that ships inside Clueless.app, at
# native/build/python.
#
# The runtime is a pinned python-build-standalone release checked against a
# recorded SHA-256, with dependencies installed from requirements.lock under
# --require-hashes. Every byte that ships is therefore pinned: a compromised
# mirror or a re-uploaded package fails the build instead of reaching users.
#
# Builds into a staging directory and swaps it in only on success, so a failed
# build never leaves a half-populated runtime that packaging would bundle.
#
# To move to a new runtime: pick a release from
# https://github.com/astral-sh/python-build-standalone/releases, then update all
# three values below, taking the hash from that release's SHA256SUMS file.
set -euo pipefail

PBS_RELEASE="20261026"
PY_VERSION="3.12.16"
PBS_SHA256="fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e"

cd "$(dirname "$0")"
NATIVE="$(pwd)"
ROOT="$(cd .. && pwd)"
BUILD="$NATIVE/build"
CACHE="$BUILD/.cache"
DEST="$BUILD/python"
STAGE="$BUILD/python.staging"
LOCK="$ROOT/requirements.lock"

ARCHIVE="cpython-${PY_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${ARCHIVE//+/%2B}"

fail() { echo "build-python: $*" >&2; exit 1; }

[ "$(uname -m)" = "arm64" ] || fail "must run on Apple silicon (got $(uname -m))"
[ -f "$LOCK" ] || fail "missing $LOCK; generate it with: pip-compile --generate-hashes --allow-unsafe -o requirements.lock requirements.txt"

mkdir -p "$CACHE"
if [ ! -f "$CACHE/$ARCHIVE" ]; then
    echo "==> downloading $ARCHIVE"
    curl --fail --location --proto '=https' --tlsv1.2 --retry 3 -o "$CACHE/$ARCHIVE.part" "$URL"
    mv "$CACHE/$ARCHIVE.part" "$CACHE/$ARCHIVE"
fi

echo "==> verifying sha256"
ACTUAL="$(shasum -a 256 "$CACHE/$ARCHIVE" | awk '{print $1}')"
if [ "$ACTUAL" != "$PBS_SHA256" ]; then
    rm -f "$CACHE/$ARCHIVE"
    fail "checksum mismatch for $ARCHIVE (expected $PBS_SHA256, got $ACTUAL); the cached copy was deleted"
fi

echo "==> extracting"
rm -rf "$STAGE"
mkdir -p "$STAGE"
# The archive's top-level directory is python/.
tar -xzf "$CACHE/$ARCHIVE" -C "$STAGE" --strip-components=1
PY="$STAGE/bin/python3"
"$PY" -c "import sys; assert sys.version.startswith('$PY_VERSION'), sys.version"

echo "==> installing requirements.lock"
# --only-binary: nothing is compiled on the build machine, so the result does
# not depend on whatever toolchain or headers happen to be installed.
PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONNOUSERSITE=1 "$PY" -m pip install \
    --no-cache-dir --no-input --require-hashes --only-binary=:all: --no-deps \
    -r "$LOCK"

echo "==> stripping"
STDLIB="$STAGE/lib/python${PY_VERSION%.*}"
rm -rf \
    "$STDLIB/test" "$STDLIB/idlelib" "$STDLIB/turtledemo" "$STDLIB/tkinter" \
    "$STDLIB/ensurepip" "$STDLIB/lib2to3" "$STDLIB"/lib-dynload/_tkinter* \
    "$STDLIB"/site-packages/pip "$STDLIB"/site-packages/pip-*.dist-info \
    "$STAGE/include" "$STAGE/share" \
    "$STAGE"/lib/tcl* "$STAGE"/lib/tk* "$STAGE"/lib/itcl* "$STAGE"/lib/thread* \
    "$STAGE"/lib/libtcl* "$STAGE"/lib/libtk* \
    "$STAGE"/bin/idle* "$STAGE"/bin/pydoc* "$STAGE"/bin/pip* "$STAGE"/bin/2to3*
# Package test suites ship in some wheels (numpy's alone is ~20 MB).
find "$STDLIB/site-packages" -type d \( -name tests -o -name testing \) -prune -exec rm -rf {} +
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +

echo "==> smoke test"
# -B, not PYTHONDONTWRITEBYTECODE: -I ignores PYTHON* variables.
"$PY" -I -B -c "
import sys
import fastapi, uvicorn, websockets, pydantic, numpy, dotenv, certifi, sqlite3, ssl
print('ok', sys.version.split()[0], 'numpy', numpy.__version__, ssl.OPENSSL_VERSION)
"

rm -rf "$DEST"
mv "$STAGE" "$DEST"
echo "==> done: $DEST ($(du -sh "$DEST" | awk '{print $1}'))"
