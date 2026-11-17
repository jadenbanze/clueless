"""Builds and updates the local retrieval index across Developer & general sources.

Developer sources:
  * Jira       — issues, epic parent, priority, issue links (blockers) & recent comments
  * Confluence — pages ordered by recency (CQL), split by section headings with overlap
  * GitHub     — shallow-fetched repositories (symbol-aware chunks) + Pull Requests

Universal / Academic / General sources live in ``server/context_sources.py`` and
are orchestrated together by ``build_all()``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import config, context_sources, harvest_corrections, rag, tls

log = logging.getLogger(__name__)

CACHE_DIR = config.DATA_DIR / ".cache" / "repos"
MANIFEST_PATH = config.DATA_DIR / ".cache" / "index-manifest.json"

NetworkIndexingDisabled = context_sources.NetworkIndexingDisabled
_require_network = context_sources._require_network

_SYMBOL_BOUNDARY = re.compile(
    r"^(?:export\s+)?(?:public\s+|private\s+|protected\s+|async\s+|abstract\s+|final\s+)*"
    r"(?:class|interface|enum|record|struct|def|function|resource|module|variable)\b"
)


def _manifest_path() -> Path:
    return config.DATA_DIR / ".cache" / "index-manifest.json"


def load_manifest() -> dict:
    try:
        return json.loads(_manifest_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(manifest: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=1))


# ---------------------------------------------------------------- Jira


def _jira_auth_header() -> str:
    if not config.JIRA_SITE:
        raise RuntimeError("JIRA_SITE is not configured")
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_API_TOKEN")
    if not email or not token:
        raise RuntimeError(
            "Jira credentials missing. Add ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN in "
            "Settings → Context Sources → Credentials (or .env)."
        )
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {encoded}"


def fetch_jira_issues(max_results: int | None = None) -> list[dict]:
    _require_network()
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
    fields = [
        "summary", "description", "status", "assignee", "labels",
        "issuetype", "priority", "parent", "issuelinks", "comment",
        "fixVersions", "updated",
    ]

    collected: list[dict] = []
    token: str | None = None
    while len(collected) < limit:
        payload: dict = {"jql": jql, "maxResults": min(100, limit - len(collected)), "fields": fields}
        if token:
            payload["nextPageToken"] = token

        request = urllib.request.Request(  # noqa: S310
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
            with urllib.request.urlopen(  # noqa: S310  # nosec B310
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
        if node.get("type") in {"paragraph", "heading", "listItem", "codeBlock"}:
            return joined + "\n"
        return joined
    return ""


def format_jira_issue(issue: dict, site: str = "") -> tuple[str, str, dict]:
    """Formats a Jira issue with parent epic, blockers/links, and recent comments."""
    key = issue.get("key", "")
    fields = issue.get("fields") or {}
    summary = fields.get("summary") or ""
    status = (fields.get("status") or {}).get("name", "")
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    parent = fields.get("parent") or {}
    parent_key = parent.get("key", "")
    parent_sum = ((parent.get("fields") or {}).get("summary") or "")[:80]

    links_out: list[str] = []
    for link in (fields.get("issuelinks") or [])[:8]:
        ltype = link.get("type") or {}
        if "outwardIssue" in link:
            target = link["outwardIssue"].get("key", "")
            rel = ltype.get("outward", "relates to")
            if target:
                links_out.append(f"{rel} {target}")
        elif "inwardIssue" in link:
            target = link["inwardIssue"].get("key", "")
            rel = ltype.get("inward", "related from")
            if target:
                links_out.append(f"{rel} {target}")

    comments_raw = ((fields.get("comment") or {}).get("comments") or [])[-5:]
    comments_out: list[str] = []
    for comment in comments_raw:
        author = ((comment.get("author") or {}).get("displayName") or "Comment")
        cbody = adf_to_text(comment.get("body")).strip()
        if cbody:
            comments_out.append(f"- {author}: {cbody[:320]}")

    description = adf_to_text(fields.get("description")).strip()[:2400]
    labels = ", ".join(fields.get("labels") or [])
    versions = ", ".join(v.get("name", "") for v in (fields.get("fixVersions") or []) if v.get("name"))

    header_bits = [f"Type: {issue_type}", f"Status: {status}", f"Assignee: {assignee}"]
    if priority:
        header_bits.append(f"Priority: {priority}")
    lines = [f"{key}: {summary}", " | ".join(header_bits)]
    if parent_key:
        lines.append(f"Parent/Epic: {parent_key}" + (f" ({parent_sum})" if parent_sum else ""))
    if links_out:
        lines.append(f"Links: {', '.join(links_out)}")
    if labels:
        lines.append(f"Labels: {labels}")
    if versions:
        lines.append(f"Target release: {versions}")
    if description:
        lines.append(f"\n{description}")
    if comments_out:
        lines.append("\nRecent comments:\n" + "\n".join(comments_out))

    meta: dict = {
        "source": "jira",
        "key": key,
        "summary": summary,
        "status": status,
        "assignee": assignee,
        "type": issue_type,
    }
    if site and key:
        meta["url"] = f"https://{site}/browse/{key}"
    return key, "\n".join(lines), meta


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
        if not key:
            continue
        fields_updated = (issue.get("fields") or {}).get("updated", "")
        seen[key] = fields_updated
        if incremental and manifest.get("jira", {}).get(key) == fields_updated and fields_updated:
            skipped += 1
            continue
        item_id, body, meta = format_jira_issue(issue, site=config.JIRA_SITE)
        ids.append(item_id)
        texts.append(body)
        metadatas.append(meta)

    for start in range(0, len(ids), 64):
        store.add(
            config.JIRA_COLLECTION,
            ids[start : start + 64],
            texts[start : start + 64],
            metadatas[start : start + 64],
        )
    if seen:
        store.delete_stale(config.JIRA_COLLECTION, set(seen.keys()))
    if skipped:
        log.info("jira: %d unchanged, %d re-indexed", skipped, len(ids))
    manifest["jira"] = seen
    save_manifest(manifest)
    return len(ids)


# ---------------------------------------------------------------- Confluence


def _confluence_get(path: str) -> dict:
    _require_network()
    request = urllib.request.Request(
        f"https://{config.JIRA_SITE}{path}",
        headers={
            "Authorization": _jira_auth_header(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request, timeout=60, context=tls.ssl_context()
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Confluence returned {error.code}: {detail}") from error


def html_to_text(html: str) -> str:
    return context_sources.strip_html(html)


def html_sections(html: str) -> list[tuple[str, str]]:
    """Splits Confluence storage HTML along h1-h4 headings with readable plain text."""
    if not html:
        return []
    cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    tokens = re.split(r"(?is)(<h[1-4][^>]*>.*?</h[1-4]>)", cleaned)
    sections: list[tuple[str, str]] = []
    current_heading = ""
    buf: list[str] = []

    def flush() -> None:
        raw = "".join(buf)
        text = html_to_text(raw)
        if text:
            sections.append((current_heading, text))

    for part in tokens:
        if re.match(r"(?is)^<h[1-4]\b", part):
            flush()
            buf.clear()
            current_heading = html_to_text(part)[:90]
        else:
            buf.append(part)
    flush()
    return sections


def fetch_confluence_pages(space_key: str, max_pages: int = 400) -> list[dict]:
    pages: list[dict] = []
    start = 0
    page_size = 50
    cql = urllib.parse.quote(f'space = "{space_key}" AND type = page ORDER BY lastmodified DESC')
    while len(pages) < max_pages:
        try:
            data = _confluence_get(
                f"/wiki/rest/api/content/search?cql={cql}"
                f"&limit={page_size}&start={start}&expand=body.storage,version,space"
            )
        except RuntimeError:
            data = _confluence_get(
                f"/wiki/rest/api/content?spaceKey={urllib.parse.quote(space_key)}"
                f"&type=page&status=current&limit={page_size}&start={start}"
                "&expand=body.storage,version,space"
            )
        batch = data.get("results", [])
        pages.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return pages[:max_pages]


def index_confluence(store: rag.Store, manifest: dict | None = None, incremental: bool = True) -> int:
    manifest = manifest if manifest is not None else load_manifest()
    prev_meta: dict[str, dict | str] = manifest.get("confluence", {})
    seen: dict[str, dict] = {}
    keep_ids: set[str] = set()
    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []
    skipped = 0

    if not config.JIRA_SITE or not config.CONFLUENCE_SPACES:
        log.info("confluence: not configured (JIRA_SITE / CONFLUENCE_SPACES), skipping")
        return 0
    for space_key in config.CONFLUENCE_SPACES:
        try:
            pages = fetch_confluence_pages(space_key, config.CONFLUENCE_MAX_PAGES)
        except Exception as error:
            log.warning("confluence space %s unavailable: %s", space_key, error)
            continue

        log.info("confluence %s: %d pages", space_key, len(pages))
        for page in pages:
            title = page.get("title", "")
            page_id = str(page.get("id") or "")
            if not page_id:
                continue
            version = str(((page.get("version") or {}).get("when")) or "")
            prev_entry = prev_meta.get(page_id)
            if (
                incremental
                and version
                and isinstance(prev_entry, dict)
                and prev_entry.get("v") == version
                and prev_entry.get("ids")
            ):
                skipped += 1
                seen[page_id] = prev_entry
                keep_ids.update(prev_entry["ids"])
                continue

            raw_html = (page.get("body") or {}).get("storage", {}).get("value", "")
            sections = html_sections(raw_html)
            if not sections:
                continue

            webui = ((page.get("_links") or {}).get("webui") or "")
            url = f"https://{config.JIRA_SITE}/wiki{webui}" if webui.startswith("/") else f"https://{config.JIRA_SITE}/wiki/spaces/{space_key}/pages/{page_id}"
            page_chunk_ids: list[str] = []

            chunk_idx = 0
            for section_title, sec_body in sections:
                for piece in context_sources._chunk_text_windows(sec_body, max_chars=2400, overlap=220):
                    if len(piece.strip()) < 60:
                        continue
                    cid = f"conf:{page_id}:{chunk_idx}"
                    chunk_idx += 1
                    breadcrumb = f"{space_key} › {title}" + (f" › {section_title}" if section_title else "")
                    page_chunk_ids.append(cid)
                    keep_ids.add(cid)
                    ids.append(cid)
                    texts.append(f"{breadcrumb}\n\n{piece}")
                    metadatas.append({
                        "source": "confluence",
                        "space": space_key,
                        "page_id": page_id,
                        "title": title,
                        "section": section_title,
                        "url": url,
                    })

            seen[page_id] = {"v": version, "ids": page_chunk_ids}

    if skipped:
        log.info("confluence: %d unchanged, %d chunks rebuilt", skipped, len(ids))
    if ids:
        for start in range(0, len(ids), 64):
            store.add(
                config.CONFLUENCE_COLLECTION,
                ids[start : start + 64],
                texts[start : start + 64],
                metadatas[start : start + 64],
            )
    if keep_ids:
        store.delete_stale(config.CONFLUENCE_COLLECTION, keep_ids)
    manifest["confluence"] = seen
    save_manifest(manifest)
    return len(ids)


# ---------------------------------------------------------------- GitHub (Code + Pull Requests)


def _gh_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    cmd = ["gh", "auth", "token"]
    if config.GITHUB_ACCOUNT:
        cmd.extend(["-u", config.GITHUB_ACCOUNT])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "No GitHub token. Add GITHUB_TOKEN in Settings → Context Sources → Credentials "
            "(or run `gh auth login`)."
        )
    return result.stdout.strip()


def _git_env(cache_dir: Path, token: str) -> dict[str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    askpass = cache_dir / ".askpass.sh"
    askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) echo x-access-token ;; *) echo "$GH_TOKEN" ;; esac\n')
    askpass.chmod(0o700)
    return dict(os.environ, GH_TOKEN=token, GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")


def sync_repo(repo: str, token: str, refresh: bool) -> Path:
    _require_network()
    cache_dir = config.DATA_DIR / ".cache" / "repos"
    target = cache_dir / repo
    env = _git_env(cache_dir, token)

    if refresh and target.exists():
        shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        # Fetch latest shallow HEAD so incremental runs detect upstream commits.
        fetch = subprocess.run(
            ["git", "-C", str(target), "fetch", "--depth", "1", "--quiet", "origin", "HEAD"],
            capture_output=True, text=True, env=env, timeout=90,
        )
        if fetch.returncode == 0:
            subprocess.run(
                ["git", "-C", str(target), "reset", "--hard", "--quiet", "FETCH_HEAD"],
                capture_output=True, text=True, env=env, timeout=30,
            )
        return target

    log.info("cloning %s/%s (shallow)…", config.GITHUB_ORG, repo)
    result = subprocess.run(
        [
            "git", "clone", "--depth", "1", "--quiet",
            f"https://github.com/{config.GITHUB_ORG}/{repo}.git", str(target),
        ],
        capture_output=True, text=True, env=env, timeout=180,
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
    """Splits a file into symbol-aware windows while keeping line numbers & URLs."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not content.strip():
        return []

    lines = content.splitlines()
    relative = str(path.relative_to(root))
    chunks: list[tuple[str, str, dict]] = []
    max_lines = config.CODE_CHUNK_LINES
    overlap = config.CODE_CHUNK_OVERLAP_LINES

    # Find top-level symbol starts so functions/classes stay intact where possible.
    boundaries = [idx for idx, line in enumerate(lines) if _SYMBOL_BOUNDARY.match(line)]
    windows: list[tuple[int, int]] = []
    if len(boundaries) >= 2:
        start = 0
        for b in boundaries[1:]:
            if b - start >= max_lines // 3:
                while b - start > max_lines:
                    windows.append((start, start + max_lines))
                    start += max_lines - overlap
                windows.append((start, b))
                start = b
        while len(lines) - start > max_lines:
            windows.append((start, start + max_lines))
            start += max_lines - overlap
        if start < len(lines):
            windows.append((start, len(lines)))
    else:
        step = max_lines - overlap
        for start in range(0, max(len(lines), 1), step):
            end = min(len(lines), start + max_lines)
            windows.append((start, end))
            if end >= len(lines):
                break

    for start, end in windows:
        window = lines[start:end]
        if not window:
            continue
        text = f"{repo}/{relative} (lines {start + 1}-{start + len(window)})\n" + "\n".join(window)
        chunk_id = f"{repo}:{relative}:{start}"
        meta: dict = {
            "source": "code",
            "kind": "file",
            "repo": repo,
            "path": relative,
            "start_line": start + 1,
        }
        if config.GITHUB_ORG:
            meta["url"] = f"https://github.com/{config.GITHUB_ORG}/{repo}/blob/HEAD/{relative}#L{start + 1}"
        chunks.append((chunk_id, text, meta))
    return chunks


def fetch_github_prs(repo: str, token: str, max_prs: int = 25) -> list[tuple[str, str, dict]]:
    """Fetches open and recently updated pull requests via the GitHub REST API."""
    _require_network()
    url = (
        f"https://api.github.com/repos/{config.GITHUB_ORG}/{repo}/pulls"
        f"?state=all&sort=updated&direction=desc&per_page={max_prs}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Clueless/0.2",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls.ssl_context()) as resp:  # noqa: S310  # nosec B310
            pulls = json.loads(resp.read().decode("utf-8"))
    except Exception as error:
        log.warning("github PR fetch failed for %s: %s", repo, error)
        return []

    out: list[tuple[str, str, dict]] = []
    for pr in pulls if isinstance(pulls, list) else []:
        num = pr.get("number")
        title = str(pr.get("title") or "")
        if not num or not title:
            continue
        merged = bool(pr.get("merged_at"))
        draft = bool(pr.get("draft"))
        raw_state = str(pr.get("state") or "open").upper()
        state = "MERGED" if merged else ("DRAFT" if draft else raw_state)
        author = str((pr.get("user") or {}).get("login") or "unknown")
        head_ref = str((pr.get("head") or {}).get("ref") or "")
        base_ref = str((pr.get("base") or {}).get("ref") or "")
        reviewers = ", ".join(
            r.get("login", "") for r in (pr.get("requested_reviewers") or []) if r.get("login")
        )
        body_text = str(pr.get("body") or "").strip()[:1600]
        updated = str(pr.get("updated_at") or "")[:10]
        html_url = str(pr.get("html_url") or f"https://github.com/{config.GITHUB_ORG}/{repo}/pull/{num}")

        doc = (
            f"{repo} PR #{num}: {title}\n"
            f"State: {state} | Author: {author} | Branch: {head_ref} -> {base_ref} | Updated: {updated}\n"
            + (f"Reviewers: {reviewers}\n" if reviewers else "")
            + (f"\n{body_text}" if body_text else "")
        )
        out.append((
            f"pr:{repo}:{num}",
            doc,
            {
                "source": "code",
                "kind": "pr",
                "repo": repo,
                "pr_number": num,
                "title": title,
                "state": state,
                "updated": str(pr.get("updated_at") or ""),
                "url": html_url,
            },
        ))
    return out


def index_github(
    store: rag.Store, refresh: bool = False, manifest: dict | None = None, incremental: bool = True
) -> int:
    _require_network()
    if not config.GITHUB_ORG or not config.GITHUB_REPOS:
        log.info("github: not configured (GITHUB_ORG / GITHUB_REPOS), skipping")
        return 0
    token = _gh_token()
    manifest = manifest if manifest is not None else load_manifest()
    prev_gh: dict = manifest.get("github", {})
    prev_prs: dict = manifest.get("github_prs", {})
    seen_gh: dict[str, str] = {}
    seen_prs: dict[str, str] = {}
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
        seen_gh[repo] = head

        ids: list[str] = []
        texts: list[str] = []
        metadatas: list[dict] = []

        if not (incremental and head and prev_gh.get(repo) == head):
            for path in iter_code_files(root):
                for chunk_id, text, metadata in chunk_file(path, root, repo):
                    ids.append(chunk_id)
                    texts.append(text)
                    metadatas.append(metadata)
        else:
            log.info("%s: code unchanged (%s)", repo, head[:8])

        # Always check recent PRs (1 lightweight REST call per repo) so PR statuses stay current.
        for pr_id, pr_text, pr_meta in fetch_github_prs(repo, token):
            updated = pr_meta.get("updated", "")
            seen_prs[pr_id] = updated
            if incremental and updated and prev_prs.get(pr_id) == updated:
                continue
            ids.append(pr_id)
            texts.append(pr_text)
            metadatas.append(pr_meta)

        for start in range(0, len(ids), 64):
            store.add(
                config.CODE_COLLECTION,
                ids[start : start + 64],
                texts[start : start + 64],
                metadatas[start : start + 64],
            )
        if ids:
            log.info("%s: indexed %d updated chunks/PRs", repo, len(ids))
        total += len(ids)

    manifest["github"] = seen_gh
    manifest["github_prs"] = seen_prs
    save_manifest(manifest)
    return total


# ---------------------------------------------------------------- Orchestrator


def build_all(
    refresh: bool = False,
    skip_jira: bool = False,
    incremental: bool = True,
    sources: list[str] | None = None,
    on_progress: Callable[[str], None] | None = None,
    store: rag.Store | None = None,
) -> dict:
    """Indexes configured sources, respecting offline-only clinical guarantees."""
    store = store or rag.Store()
    manifest = load_manifest()
    wanted = set(sources) if sources else set(config.SOURCE_COLLECTIONS.keys())
    result: dict[str, int | str | float] = {src: 0 for src in config.SOURCE_COLLECTIONS}

    def step(label: str) -> None:
        if on_progress:
            on_progress(label)

    # 1. Offline local sources — safe under every profile including Clinical mode.
    if "notes" in wanted and config.LOCAL_NOTES_DIRS:
        step("Indexing local files…")
        result["notes"] = context_sources.index_local_notes(store, manifest, incremental=not refresh and incremental)
    if "anki" in wanted and config.ANKI_PATH:
        step("Indexing Anki flashcards…")
        result["anki"] = context_sources.index_anki(store, manifest, incremental=not refresh and incremental)

    # 2. Network sources — strictly skipped when NETWORK_INDEXING is False.
    if not config.NETWORK_INDEXING:
        # Offline Zotero .bib/.ris is still local-only if configured without PubMed network fetch.
        if "pubmed" in wanted and config.ZOTERO_BIB_PATH:
            step("Indexing local bibliography…")
            result["pubmed"] = context_sources.index_pubmed(store, manifest, incremental=not refresh and incremental)
        result["network_skipped"] = f"network indexing disabled for {config.PROFILE}"
    else:
        if "jira" in wanted and not skip_jira and config.JIRA_SITE and config.JIRA_PROJECT_KEY:
            step("Syncing Jira issues…")
            result["jira"] = index_jira(store, manifest, incremental=not refresh and incremental)
        if "confluence" in wanted and config.JIRA_SITE and config.CONFLUENCE_SPACES:
            step("Syncing Confluence pages…")
            result["confluence"] = index_confluence(store, manifest, incremental=not refresh and incremental)
        if "code" in wanted and config.GITHUB_ORG and config.GITHUB_REPOS:
            step("Syncing GitHub repositories & PRs…")
            result["code"] = index_github(store, refresh=refresh, manifest=manifest, incremental=incremental)
        if "pubmed" in wanted and (config.PUBMED_QUERY or config.ZOTERO_BIB_PATH):
            step("Syncing PubMed & bibliography…")
            result["pubmed"] = context_sources.index_pubmed(store, manifest, incremental=not refresh and incremental)
        if "canvas" in wanted and config.CANVAS_URL:
            step("Syncing Canvas LMS…")
            result["canvas"] = context_sources.index_canvas(store, manifest, incremental=not refresh and incremental)
        if "notion" in wanted and os.environ.get("NOTION_TOKEN"):
            step("Syncing Notion pages…")
            result["notion"] = context_sources.index_notion(store, manifest, incremental=not refresh and incremental)

    now_ts = time.time()
    manifest["last_synced_at"] = now_ts
    save_manifest(manifest)

    # Refresh auto-harvested speech vocabulary from newly indexed metadata + calendar.
    try:
        _cal_text, attendees = context_sources.get_calendar_context()
        harvest_corrections.harvest_to_cache(extra_names=attendees)
    except Exception as error:
        log.debug("post-index vocab harvest skipped: %s", error)

    counts = store.counts()
    result["totals"] = {src: counts.get(col, 0) for src, col in config.SOURCE_COLLECTIONS.items()}
    result["jira_total"] = counts.get(config.JIRA_COLLECTION, 0)
    result["confluence_total"] = counts.get(config.CONFLUENCE_COLLECTION, 0)
    result["code_total"] = counts.get(config.CODE_COLLECTION, 0)
    result["synced_at"] = now_ts
    return result
