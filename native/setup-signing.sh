#!/usr/bin/env bash
# Creates a stable, self-signed code-signing identity for the audio helper.
#
# Why this exists: ad-hoc signing (`codesign --sign -`) gives the binary no
# stable code identity, so TCC pins the audio-capture grant to the binary's
# CDHash. Any source change produces a new CDHash, macOS treats it as a
# different app, and the user is prompted for permission all over again.
#
# Signing with a real (even self-signed) certificate makes the designated
# requirement depend on the bundle identifier plus the certificate, both of
# which are stable across rebuilds — so the grant is given once and persists.
#
# Idempotent: re-running when the identity already exists is a no-op.
set -euo pipefail

CERT_NAME="Clueless Dev"
KEYCHAIN="${HOME}/Library/Keychains/login.keychain-db"

if security find-certificate -c "${CERT_NAME}" "${KEYCHAIN}" >/dev/null 2>&1; then
    echo "identity '${CERT_NAME}' already present — nothing to do"
    security find-identity -v -p codesigning | grep "${CERT_NAME}" || true
    exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "==> generating self-signed code-signing certificate"
# extendedKeyUsage=codeSigning is what makes codesign accept the identity.
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "${WORK}/key.pem" -out "${WORK}/cert.pem" \
    -subj "/CN=${CERT_NAME}/O=Clueless/C=US" \
    -addext "basicConstraints=critical,CA:false" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=critical,codeSigning" 2>/dev/null

echo "==> bundling into PKCS#12"
# OpenSSL 3 defaults to an AES/SHA-256 PKCS#12 MAC that macOS's Security
# framework rejects with "MAC verification failed". The legacy SHA-1/3DES
# algorithms are required for `security import` to accept the file.
P12_PASS="transient-$$"
openssl pkcs12 -export -legacy -macalg sha1 \
    -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES \
    -out "${WORK}/identity.p12" \
    -inkey "${WORK}/key.pem" -in "${WORK}/cert.pem" \
    -name "${CERT_NAME}" -passout "pass:${P12_PASS}"

echo "==> importing into the login keychain"
# -T /usr/bin/codesign pre-authorizes codesign to use the private key, so
# signing does not pop a keychain-access dialog on every build.
security import "${WORK}/identity.p12" -k "${KEYCHAIN}" -P "${P12_PASS}" \
    -T /usr/bin/codesign -T /usr/bin/security

echo "==> marking the certificate as trusted for code signing"
# User-domain trust only: no sudo, no change to system-wide trust policy.
security add-trusted-cert -r trustRoot -p codeSign \
    -k "${KEYCHAIN}" "${WORK}/cert.pem" 2>/dev/null \
    || echo "    (trust settings unchanged; codesign usually works regardless)"

echo "==> allowing codesign to access the key non-interactively"
security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k "" "${KEYCHAIN}" >/dev/null 2>&1 \
    || echo "    (could not set partition list; you may get one keychain prompt)"

echo
echo "==> resulting identities:"
security find-identity -v -p codesigning
