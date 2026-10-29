"""TLS trust for outbound requests on this machine.

This network intercepts TLS, so a request to Jira fails with
CERTIFICATE_VERIFY_FAILED against Python's bundled CA list — the intercepting
root simply is not in it.

The fix is to trust what the operating system trusts, not to disable
verification. A combined bundle is assembled from the macOS keychains and cached;
`ssl` then verifies against the same roots every other app on the machine uses.
Certificate verification stays on, which matters because these requests carry an
API token.
"""

from __future__ import annotations

import logging
import ssl
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

BUNDLE_PATH = config.PROJECT_ROOT / ".cache" / "ca-bundle.pem"

# Apple's own roots, plus anything installed by the organisation (the corporate
# interception root lives in the System keychain).
KEYCHAINS = (
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",
)


def _export_keychain(path: str) -> str:
    result = subprocess.run(
        ["security", "find-certificate", "-a", "-p", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.debug("could not export %s: %s", path, result.stderr.strip())
        return ""
    return result.stdout


def _certifi_bundle() -> str:
    try:
        import certifi

        return Path(certifi.where()).read_text(encoding="utf-8")
    except Exception:
        return ""


def build_bundle(force: bool = False) -> Path:
    """Writes the combined CA bundle once and returns its path."""
    if BUNDLE_PATH.exists() and not force:
        return BUNDLE_PATH

    parts = [_certifi_bundle()]
    for keychain in KEYCHAINS:
        parts.append(_export_keychain(keychain))

    combined = "\n".join(part for part in parts if part.strip())
    if not combined.strip():
        raise RuntimeError(
            "could not assemble a CA bundle from the macOS keychains; "
            "TLS verification against the corporate proxy will fail"
        )

    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(combined, encoding="utf-8")
    count = combined.count("BEGIN CERTIFICATE")
    log.info("built CA bundle with %d certificates at %s", count, BUNDLE_PATH)
    return BUNDLE_PATH


def ssl_context() -> ssl.SSLContext:
    """A verifying context that trusts the machine's roots."""
    return ssl.create_default_context(cafile=str(build_bundle()))
