"""Builds the local retrieval index from Jira and GitHub.

Jira needs its own credentials: the Atlassian MCP server used interactively by an
agent is not available to this process, so the indexer talks to the REST API
directly. Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN.

GitHub uses the `gh` CLI. If the org is only visible to a second account, set
GITHUB_ACCOUNT so the token is fetched for that account explicitly.

Scope is deliberately narrow. The Jira instance has >1000 projects; indexing all
of them would take hours and bury relevant tickets in noise, so the JQL is
limited to this project and the current sprint.
"""

from __future__ import annotations

import base64
import html as html_module
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import config, rag, tls

log = logging.getLogger(__name__)

CACHE_DIR = config.PROJECT_ROOT / ".cache" / "repos"
MANIFEST_PATH = config.PROJECT_ROOT / ".cache" / "index-manifest.json"


def load_manifest() -> dict:
    """Per-item fingerprints from the last successful index.

    Rebuilds were taking ~8 minutes because every ticket, page and file was
    re-embedded whether or not it had changed. The manifest records what was
    indexed, so a rebuild only pays for the delta.
    """
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=1))


# ---------------------------------------------------------------- Jira


def _jira_auth_header() -> str:
    if not config.JIRA_SITE:
        raise RuntimeError("JIRA_SITE is not configured")
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_API_TOKEN")
    if not email or not token:
        raise RuntimeError(
            "Jira credentials missing. Put ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN in "
            f"{config.PROJECT_ROOT / '.env'} (see .env.example), or export them in the "
            "environment. Create a token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens"
        )
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {encoded}"


def fetch_jira_issues(max_results: int | None = None) -> list[dict]:
    # Scope: everything in the project that is either recent, mine, in the open
    # sprint, or explicitly unassigned and still open.
    #
    # The original `assignee = currentUser() OR sprint in openSprints()` was too
    # narrow: an unassigned ticket that was not in the sprint existed in Jira but
    # was never indexed, so the assistant correctly reported "no ticket" for a
    # ticket that did exist.
    jql = (
        f"project = {config.JIRA_PROJECT_KEY} "
        "AND ("
        "  assignee = currentUser()"
        "  OR sprint in openSprints()"
        "  OR updated >= -90d"
        "  OR (assignee is EMPTY AND resolution = Unresolved)"
        ") "
        "ORDER BY updated DESC"
    )
    url = f"https://{config.JIRA_SITE}/rest/api/3/search/jql"
    limit = max_results or config.JIRA_MAX_ISSUES
    fields = ["summary", "description", "status", "assignee", "labels",
              "issuetype", "priority", "parent", "updated"]

    collected: list[dict] = []
    token: str | None = None
    # Jira caps a page at 100 and hands back a nextPageToken, so a single call
    # silently truncates — the widened JQL still returned exactly 100 issues.
    while len(collected) < limit:
        payload = {"jql": jql, "maxResults": min(100, limit - len(collected)), "fields": fields}
        if token:
            payload["nextPageToken"] = token

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": _jira_auth_header(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            # Verify against the machine's own roots: this network intercepts
            # TLS, and Python's bundled CA list does not include the root.
            with urllib.request.urlopen(
                request, timeout=60, context=tls.ssl_context()
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Jira returned {error.code}: {detail}") from error

        page = data.get("issues", [])
        collected.extend(page)
        token = data.get("nextPageToken")
        if not page or not token:
            break

    log.info("fetched %d Jira issues", len(collected))
    return collected


def adf_to_text(node) -> str:
    """Flattens Atlassian Document Format into plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(adf_to_text(item) for item in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [adf_to_text(child) for child in node.get("content", [])]
        joined = " ".join(p for p in parts if p)
        # Block-level nodes read better separated by newlines.
        if node.get("type") in {"paragraph", "heading", "listItem", "codeBlock"}:
            return joined + "\n"
        return joined
    return ""


def index_jira(store: rag.Store, manifest: dict | None = None, incremental: bool = True) -> int:
    if not config.JIRA_SITE or not config.JIRA_PROJECT_KEY:
        log.info("jira: not configured (JIRA_SITE / JIRA_PROJECT_KEY), skipping")
        return 0
    issues = fetch_jira_issues()
    manifest = manifest if manifest is not None else load_manifest()
    seen: dict[str, str] = {}
    ids, texts, metadatas = [], [], []
    skipped = 0

    for issue in issues:
        key = issue.get("key", "")
        fields_updated = (issue.get("fields") or {}).get("updated", "")
        seen[key] = fields_updated
        if incremental and manifest.get("jira", {}).get(key) == fields_updated and fields_updated:
            skipped += 1
            continue
        fields = issue.get("fields") or {}
        summary = fields.get("summary") or ""
        status = (fields.get("status") or {}).get("name", "")
        assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        description = adf_to_text(fields.get("description")).strip()
        labels = ", ".join(fields.get("labels") or [])

        body = (
            f"{key}: {summary}\n"
            f"Type: {issue_type} | Status: {status} | Assignee: {assignee}\n"
            + (f"Labels: {labels}\n" if labels else "")
            + (f"\n{description}" if description else "")
        )
        ids.append(key)
        texts.append(body)
        metadatas.append(
            {
                "source": "jira",
                "key": key,
                "summary": summary,
                "status": status,
                "assignee": assignee,
                "type": issue_type,
            }
        )

    store.add(config.JIRA_COLLECTION, ids, texts, metadatas)
    if skipped:
        log.info("jira: %d unchanged, %d re-indexed", skipped, len(ids))
    manifest["jira"] = seen
    save_manifest(manifest)
    return len(ids)


# ---------------------------------------------------------------- Confluence


def _confluence_get(path: str) -> dict:
    request = urllib.request.Request(
        f"https://{config.JIRA_SITE}{path}",
        headers={
            "Authorization": _jira_auth_header(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=60, context=tls.ssl_context()
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Confluence returned {error.code}: {detail}") from error


def html_to_text(html: str) -> str:
    """Flattens Confluence storage-format HTML to readable text.

    Deliberately crude: no parser dependency, and the goal is text for an
    embedding model, not faithful rendering. Script/style bodies are dropped
    first so their contents do not leak into the text.
    """
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_confluence_pages(space_key: str, max_pages: int = 400) -> list[dict]:
    pages: list[dict] = []
    start = 0
    page_size = 50
    while len(pages) < max_pages:
        data = _confluence_get(
            f"/wiki/rest/api/content?spaceKey={space_key}"
            f"&type=page&status=current&limit={page_size}&start={start}"
            "&expand=body.storage,version,space"
        )
        batch = data.get("results", [])
        pages.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return pages[:max_pages]


def index_confluence(store: "rag.Store", manifest: dict | None = None,
                     incremental: bool = True) -> int:
    manifest = manifest if manifest is not None else load_manifest()
    seen: dict[str, str] = {}
    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []
    skipped = 0

    if not config.JIRA_SITE or not config.CONFLUENCE_SPACES:
        log.info("confluence: not configured (JIRA_SITE / CONFLUENCE_SPACES), skipping")
        return 0
    for space_key in config.CONFLUENCE_SPACES:
        try:
            pages = fetch_confluence_pages(space_key)
        except Exception as error:
            log.warning("confluence space %s unavailable: %s", space_key, error)
            continue

        log.info("confluence %s: %d pages", space_key, len(pages))
        for page in pages:
            title = page.get("title", "")
            page_id_str = str(page.get("id"))
            version = str(((page.get("version") or {}).get("when")) or "")
            seen[page_id_str] = version
            if incremental and version and manifest.get("confluence", {}).get(page_id_str) == version:
                skipped += 1
                continue
            body = html_to_text((page.get("body") or {}).get("storage", {}).get("value", ""))
            if not body.strip():
                continue

            page_id = str(page.get("id"))
            # Pages are often longer than a code chunk; split on paragraphs so a
            # single embedding does not span unrelated sections.
            step = 3000
            for offset in range(0, len(body), step):
                piece = body[offset:offset + step]
                if len(piece.strip()) < 80:
                    continue
                ids.append(f"conf:{page_id}:{offset}")
                texts.append(f"{title}\n\n{piece}")
                metadatas.append({
                    "source": "confluence",
                    "space": space_key,
                    "page_id": page_id,
                    "title": title,
                })

    if skipped:
        log.info("confluence: %d unchanged, %d chunks rebuilt", skipped, len(ids))
    manifest["confluence"] = seen
    save_manifest(manifest)
    if ids:
        batch = 64
        for start in range(0, len(ids), batch):
            store.add(
                config.CONFLUENCE_COLLECTION,
                ids[start:start + batch],
                texts[start:start + batch],
                metadatas[start:start + batch],
            )
    return len(ids)


# ---------------------------------------------------------------- GitHub


def _gh_token() -> str:
    """Resolves the GitHub token.

    Prefers GITHUB_TOKEN from the environment (i.e. .env). Falling back to
    `gh auth token` couples indexing to the gh CLI being installed, logged in,
    and its keychain entry unlocked — three things that can each fail
    independently, and the failure surfaces as an opaque clone error.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    result = subprocess.run(
        ["gh", "auth", "token", "-u", config.GITHUB_ACCOUNT],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "No GitHub token. Set GITHUB_TOKEN in .env (a classic or fine-grained "
            "PAT with 'repo' read access to your org), or run "
            f"`gh auth login` as {config.GITHUB_ACCOUNT}. "
            f"gh said: {result.stderr.strip()[:200]}"
        )
    return result.stdout.strip()


def sync_repo(repo: str, token: str, refresh: bool) -> Path:
    target = CACHE_DIR / repo
    if refresh and target.exists():
        shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        log.info("%s already cloned", repo)
        return target

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("cloning %s/%s (shallow)…", config.GITHUB_ORG, repo)
    # The token is passed through the environment, never in argv.
    #
    # Embedding it in the URL (https://x-access-token:TOKEN@github.com/...) put
    # the credential in the process command line, where any local process can
    # read it with `ps`. The askpass helper below reads it from the environment
    # instead.
    askpass = CACHE_DIR / ".askpass.sh"
    askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) echo x-access-token ;; *) echo "$GH_TOKEN" ;; esac\n')
    askpass.chmod(0o700)

    environment = dict(os.environ, GH_TOKEN=token, GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet",
         f"https://github.com/{config.GITHUB_ORG}/{repo}.git", str(target)],
        capture_output=True, text=True, env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(f"clone failed for {repo}: {result.stderr.strip()[:300]}")
    return target


def iter_code_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in config.CODE_EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in config.CODE_INCLUDE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > config.CODE_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def chunk_file(path: Path, root: Path, repo: str) -> list[tuple[str, str, dict]]:
    """Splits a file into overlapping line windows, keeping the path in each."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not content.strip():
        return []

    lines = content.splitlines()
    relative = str(path.relative_to(root))
    chunks: list[tuple[str, str, dict]] = []
    step = config.CODE_CHUNK_LINES - config.CODE_CHUNK_OVERLAP_LINES

    for start in range(0, max(len(lines), 1), step):
        window = lines[start:start + config.CODE_CHUNK_LINES]
        if not window:
            break
        text = f"{repo}/{relative} (lines {start + 1}-{start + len(window)})\n" + "\n".join(window)
        chunk_id = f"{repo}:{relative}:{start}"
        chunks.append(
            (chunk_id, text, {"source": "code", "repo": repo, "path": relative,
                              "start_line": start + 1})
        )
        if start + config.CODE_CHUNK_LINES >= len(lines):
            break
    return chunks


def index_github(store: rag.Store, refresh: bool = False,
                 manifest: dict | None = None, incremental: bool = True) -> int:
    if not config.GITHUB_ORG or not config.GITHUB_REPOS:
        log.info("github: not configured (GITHUB_ORG / GITHUB_REPOS), skipping")
        return 0
    token = _gh_token()
    manifest = manifest if manifest is not None else load_manifest()
    seen: dict[str, str] = {}
    total = 0
    for repo in config.GITHUB_REPOS:
        root = sync_repo(repo, token, refresh)

        head = ""
        try:
            head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        seen[repo] = head
        if incremental and head and manifest.get("github", {}).get(repo) == head:
            log.info("%s: unchanged (%s), skipping", repo, head[:8])
            continue
        ids, texts, metadatas = [], [], []
        for path in iter_code_files(root):
            for chunk_id, text, metadata in chunk_file(path, root, repo):
                ids.append(chunk_id)
                texts.append(text)
                metadatas.append(metadata)
        # Batch to keep individual embedding calls bounded.
        batch = 64
        for start in range(0, len(ids), batch):
            store.add(
                config.CODE_COLLECTION,
                ids[start:start + batch],
                texts[start:start + batch],
                metadatas[start:start + batch],
            )
        log.info("%s: %d chunks", repo, len(ids))
        total += len(ids)
    manifest["github"] = seen
    save_manifest(manifest)
    return total


def build_all(refresh: bool = False, skip_jira: bool = False,
              incremental: bool = True) -> dict:
    store = rag.Store()
    manifest = load_manifest()
    result = {"jira": 0, "code": 0}
    if not skip_jira:
        result["jira"] = index_jira(store, manifest, incremental)
    result["confluence"] = index_confluence(store, manifest, incremental)
    result["code"] = index_github(store, refresh, manifest, incremental)
    result["jira_total"] = store.count(config.JIRA_COLLECTION)
    result["confluence_total"] = store.count(config.CONFLUENCE_COLLECTION)
    result["code_total"] = store.count(config.CODE_COLLECTION)
    return result
