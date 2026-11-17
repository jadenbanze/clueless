---
name: privacy-security-check
description: Verify Clueless local-only model egress, loopback API guard, PHI redaction, secret isolation, and neutrality denylist
allowed-tools:
  - read
  - grep
  - glob
  - exec
---

Audit Clueless security, privacy, and open-source neutrality invariants before finishing changes:

1. Run automated security and neutrality gates from the repository root:
   - `scripts/denylist-check.sh` (ensures zero organisation-specific terms, internal keys, or personal names exist in tracked files)
   - `python3 -m server.test_security` (verifies Bearer/WebSocket token auth, Host allowlist, and browser Origin rejection)
   - `python3 -m server.test_phi` (verifies identifier redaction across clinical/redacted profiles)
   - `cd overlay && npm run lint` (enforces `eslint-plugin-security` and forbids `innerHTML` / `eval`)
2. Verify code-level invariants:
   - Model requests go exclusively through `server/providers/http.py` (`is_loopback_url` enforced unless `ALLOW_REMOTE_PROVIDER=1`)
   - Outbound context indexers call `_require_network()` so Clinical / Medical mode makes zero outbound connections
   - API tokens and `.env` secrets are never returned by `GET /settings` or logged to disk, and transcripts stay in memory only
