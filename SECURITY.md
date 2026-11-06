# Security policy

Clueless listens to meetings, so a security bug here can expose conversations.
Reports are taken seriously and handled privately.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting:
*Security → Report a vulnerability* on this repository. Include:

- what an attacker can do, and what they need first (a malicious web page,
  local code execution, a crafted transcript, …)
- steps to reproduce, ideally against the latest release
- the version (`Settings… → About`, or `overlay/package.json`)

You should get an acknowledgement within 3 working days and a fix or mitigation
plan within 30 days. Credit is given in the release notes unless you ask
otherwise.

## Supported versions

Only the latest minor release receives security fixes.

## Security model

What Clueless defends against, so reports can be judged against it:

| Boundary | Protection |
|---|---|
| Web pages in your browser | The local API on `127.0.0.1:8766` requires a per-launch token, rejects any request carrying an `Origin` header, and rejects any `Host` other than loopback (DNS rebinding). The token never reaches a renderer. |
| Meeting text leaving the machine | All model providers must be on loopback; a remote URL is rejected unless `ALLOW_REMOTE_PROVIDER=1` is set in `.env`. No telemetry. Network calls happen only when you index Jira, Confluence or GitHub, and the medical profile disables those entirely. |
| Untrusted transcript / model output | Rendered with `textContent`, never HTML. Renderers are sandboxed with context isolation, cannot navigate, and cannot open windows. |
| Credentials | Kept in `.env` in your Application Support folder (`chmod 600`), never bundled, never in process arguments. The GitHub token reaches `git` through `GIT_ASKPASS`, not the clone URL. |
| Settings input | Validated against an allowlisted schema before anything is written or used. |

Out of scope: an attacker who already runs code as your user account (they can
read your microphone directly), and the models' answers themselves.

## For contributors

`pre-commit install` enables gitleaks, bandit, ruff, ESLint's security rules,
and a denylist hook for organisation-specific terms. CI runs the same checks
plus `pip-audit`, `npm audit` and CodeQL; see [CONTRIBUTING.md](CONTRIBUTING.md).
