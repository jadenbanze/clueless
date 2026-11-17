"""Neutral context providers for students, researchers, clinicians and general users.

Supported sources:
  * Local files (``notes``)    — .pdf (via PDFKit), .pptx, .docx, .md (Obsidian), .txt, .csv (offline)
  * Anki decks (``anki``)      — .apkg packages or local Anki2 SQLite collections (offline)
  * Literature (``pubmed``)    — offline Zotero .bib/.ris + PubMed NCBI E-Utilities abstracts
  * Canvas LMS (``canvas``)    — active courses, syllabi, pages and assignments
  * Notion (``notion``)        — shared pages and database items
  * Calendar (``calendar``)    — on-device macOS EventKit bridge (current meeting & attendees)

Privacy rule: every function that opens a network socket calls ``_require_network()``
first, so Clinical mode (``NETWORK_INDEXING = False``) can still index local
reference PDFs and Anki decks on disk while making zero outbound connections.
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # noqa: S405  # nosec B405
import zipfile
from pathlib import Path

from . import config, rag, tls

log = logging.getLogger(__name__)

MAX_LOCAL_DOC_BYTES = 25 * 1024 * 1024
MAX_XML_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ANKI_CARDS = 3000


class NetworkIndexingDisabled(RuntimeError):
    """Raised instead of making any outbound request when network indexing is off."""


def _require_network() -> None:
    if not config.NETWORK_INDEXING:
        raise NetworkIndexingDisabled(
            f"network indexing is disabled for the {config.PROFILE} profile"
        )


def _safe_xml_root(raw_xml: bytes | str) -> ET.Element | None:
    """Parses XML after rejecting DTD/ENTITY declarations to prevent XXE/entity expansion."""
    text = raw_xml.decode("utf-8", errors="replace") if isinstance(raw_xml, bytes) else raw_xml
    upper = text[:4096].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        # PubMed efetch includes a harmless <!DOCTYPE PubmedArticleSet ...> header
        # with no <!ENTITY>; strip a bare DOCTYPE without entities, refuse any ENTITY.
        if "<!ENTITY" in upper:
            return None
        text = re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1, flags=re.IGNORECASE)
    try:
        return ET.fromstring(text)  # noqa: S314  # nosec B314
    except ET.ParseError:
        return None


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _chunk_text_windows(text: str, max_chars: int = 1800, overlap: int = 200) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    chunks: list[str] = []
    step = max(400, max_chars - overlap)
    for start in range(0, len(clean), step):
        piece = clean[start : start + max_chars].strip()
        if len(piece) >= 40:
            chunks.append(piece)
        if start + max_chars >= len(clean):
            break
    return chunks


# ---------------------------------------------------------------- 1. Local Notes / PDFs / Slides / DOCX


def _extract_pdf_pages(path: Path) -> list[tuple[str, str]]:
    """Extracts (location_label, text) per page via the native PDFKit helper."""
    if not config.PDF_BINARY.exists():
        log.debug("PDF helper not built at %s; skipping %s", config.PDF_BINARY, path.name)
        return []
    try:
        result = subprocess.run(
            [str(config.PDF_BINARY), str(path), "--max-pages", "250"],
            capture_output=True, text=True, timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    pages: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = str(obj.get("text") or "").strip()
        page_num = obj.get("page")
        if text:
            pages.append((f"p. {page_num}", text))
    return pages


def _extract_pptx_slides(path: Path) -> list[tuple[str, str]]:
    """Extracts (slide_label, text) from a .pptx archive in memory."""
    slides: list[tuple[int, str]] = []
    try:
        with zipfile.ZipFile(path, "r") as archive:
            for info in archive.infolist():
                match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", info.filename)
                if not match or info.file_size > MAX_XML_MEMBER_BYTES:
                    continue
                slide_no = int(match.group(1))
                root = _safe_xml_root(archive.read(info.filename))
                if root is None:
                    continue
                runs = [elem.text.strip() for elem in root.iter() if elem.tag.endswith("}t") and elem.text and elem.text.strip()]
                if runs:
                    slides.append((slide_no, "\n".join(runs)))
    except (OSError, zipfile.BadZipFile):
        return []
    slides.sort(key=lambda item: item[0])
    return [(f"slide {num}", body) for num, body in slides]


def _extract_docx_sections(path: Path) -> list[tuple[str, str]]:
    """Extracts paragraph chunks from a .docx archive in memory."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_XML_MEMBER_BYTES:
                return []
            root = _safe_xml_root(archive.read("word/document.xml"))
    except (OSError, KeyError, zipfile.BadZipFile):
        return []
    if root is None:
        return []

    paragraphs: list[str] = []
    for elem in root.iter():
        if elem.tag.endswith("}p"):
            parts = [t.text for t in elem.iter() if t.tag.endswith("}t") and t.text]
            line = "".join(parts).strip()
            if line:
                paragraphs.append(line)
    full = "\n".join(paragraphs)
    return [(f"part {idx + 1}", piece) for idx, piece in enumerate(_chunk_text_windows(full))]


def _extract_markdown_sections(path: Path) -> list[tuple[str, str]]:
    """Splits Markdown / text files by headings, preserving Obsidian wikilinks."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not content.strip():
        return []
    # Strip YAML frontmatter delimiter noise while keeping title/tags readable.
    content = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", content)
    content = re.sub(r"\[\[([^\]]+)\]\]", r"\1", content)

    sections: list[tuple[str, str]] = []
    current_heading = ""
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if not body:
            return
        for piece in _chunk_text_windows(body):
            sections.append((current_heading, piece))

    for line in content.splitlines():
        heading_match = re.match(r"^(#{1,3})\s+(.+)$", line.strip())
        if heading_match:
            flush()
            buf.clear()
            current_heading = heading_match.group(2).strip()[:80]
        else:
            buf.append(line)
    flush()
    return sections


def index_local_notes(store: rag.Store, manifest: dict, incremental: bool = True) -> int:
    """Indexes local study folders, lecture PDFs, slides, DOCX and Markdown vaults."""
    if not config.LOCAL_NOTES_DIRS:
        return 0

    prev_files: dict[str, dict] = manifest.get("notes_files", {})
    next_files: dict[str, dict] = {}
    keep_ids: set[str] = set()
    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []

    for raw_dir in config.LOCAL_NOTES_DIRS:
        root = Path(raw_dir).expanduser()
        if not root.exists():
            continue
        files = [root] if root.is_file() else list(root.rglob("*"))
        for path in files:
            if not path.is_file():
                continue
            if any(part.startswith(".") or part in config.CODE_EXCLUDE_DIRS for part in path.parts):
                continue
            suffix = path.suffix.lower()
            if suffix not in config.NOTES_INCLUDE_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size == 0 or stat.st_size > MAX_LOCAL_DOC_BYTES:
                continue

            file_key = str(path.resolve())
            fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
            cached = prev_files.get(file_key)
            if incremental and cached and cached.get("fp") == fingerprint and cached.get("ids"):
                next_files[file_key] = cached
                keep_ids.update(cached["ids"])
                continue

            if suffix == ".pdf":
                units = _extract_pdf_pages(path)
            elif suffix == ".pptx":
                units = _extract_pptx_slides(path)
            elif suffix == ".docx":
                units = _extract_docx_sections(path)
            else:
                units = _extract_markdown_sections(path)

            rel_title = path.name if root.is_file() else str(path.relative_to(root))
            digest = hashlib.sha1(file_key.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
            file_ids: list[str] = []

            for idx, (loc, body) in enumerate(units):
                for sub_idx, window in enumerate(_chunk_text_windows(body)):
                    chunk_id = f"note:{digest}:{idx}:{sub_idx}"
                    header = f"{rel_title}" + (f" ({loc})" if loc else "")
                    file_ids.append(chunk_id)
                    keep_ids.add(chunk_id)
                    ids.append(chunk_id)
                    texts.append(f"{header}\n{window}")
                    metadatas.append({
                        "source": "notes",
                        "title": rel_title,
                        "location": loc,
                        "path": rel_title,
                        "local_path": file_key,
                    })

            next_files[file_key] = {"fp": fingerprint, "ids": file_ids}

    for start in range(0, len(ids), 64):
        store.add(
            config.NOTES_COLLECTION,
            ids[start : start + 64],
            texts[start : start + 64],
            metadatas[start : start + 64],
        )
    store.delete_stale(config.NOTES_COLLECTION, keep_ids)
    manifest["notes_files"] = next_files
    return len(ids)


# ---------------------------------------------------------------- 2. Offline Anki Decks


_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::(.*?))?\}\}", re.DOTALL)


def _clean_anki_field(raw: str) -> str:
    text = _CLOZE_RE.sub(r"\1", raw or "")
    text = re.sub(r"\[sound:[^\]]+\]", "", text)
    return strip_html(text)


def _resolve_anki_sqlite() -> tuple[Path | None, str | None]:
    """Returns (path_to_sqlite_copy, cleanup_dir) or (None, None)."""
    raw = config.ANKI_PATH.strip()
    if not raw:
        return None, None

    candidate: Path | None = None
    if raw.lower() == "auto":
        base = Path.home() / "Library" / "Application Support" / "Anki2"
        if base.is_dir():
            dbs = sorted(base.glob("*/collection.anki2"), key=lambda p: p.stat().st_mtime, reverse=True)
            candidate = dbs[0] if dbs else None
    else:
        p = Path(raw).expanduser()
        if p.is_file():
            candidate = p

    if candidate is None:
        return None, None

    tmpdir = tempfile.mkdtemp(prefix="clueless-anki-")
    if candidate.suffix.lower() == ".apkg":
        try:
            with zipfile.ZipFile(candidate, "r") as zf:
                member = next(
                    (n for n in ("collection.anki21", "collection.anki2") if n in zf.namelist()),
                    None,
                )
                if not member or zf.getinfo(member).file_size > 120 * 1024 * 1024:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    return None, None
                dest = Path(tmpdir) / "collection.sqlite"
                dest.write_bytes(zf.read(member))
                return dest, tmpdir
        except (OSError, zipfile.BadZipFile):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, None

    # Copy live collection so we never contend with a running Anki desktop lock.
    dest = Path(tmpdir) / "collection.sqlite"
    try:
        shutil.copy2(candidate, dest)
        return dest, tmpdir
    except OSError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, None


def index_anki(store: rag.Store, manifest: dict, incremental: bool = True) -> int:
    """Indexes Anki flashcards (.apkg or live Anki2 collection) offline."""
    if not config.ANKI_PATH:
        return 0

    db_path, cleanup_dir = _resolve_anki_sqlite()
    if db_path is None:
        return 0

    try:
        deck_filter = config.ANKI_DECK_FILTER.strip().lower()
        with sqlite3.connect(str(db_path)) as conn:
            deck_names: dict[int, str] = {}
            try:
                for did, name in conn.execute("SELECT id, name FROM decks").fetchall():
                    deck_names[int(did)] = str(name).replace("\x1f", "::")
            except sqlite3.OperationalError:
                row = conn.execute("SELECT decks FROM col LIMIT 1").fetchone()
                if row and row[0]:
                    for did_s, obj in json.loads(row[0]).items():
                        deck_names[int(did_s)] = str(obj.get("name", "Anki"))

            rows = conn.execute(
                """
                SELECT n.id, n.mod, n.flds, n.tags, MIN(c.did)
                FROM notes n
                LEFT JOIN cards c ON c.nid = n.id
                GROUP BY n.id
                ORDER BY n.mod DESC
                LIMIT ?
                """,
                (MAX_ANKI_CARDS,),
            ).fetchall()

        prev_anki: dict[str, str] = manifest.get("anki", {})
        seen: dict[str, str] = {}
        ids, texts, metadatas = [], [], []

        for nid, mod, flds, tags, did in rows:
            deck = deck_names.get(int(did or 0), "Anki")
            if deck_filter and deck_filter not in deck.lower():
                continue
            item_id = f"anki:{nid}"
            mod_str = f"{mod}:{deck}"
            seen[item_id] = mod_str
            if incremental and prev_anki.get(item_id) == mod_str:
                continue

            parts = [_clean_anki_field(p) for p in str(flds or "").split("\x1f")]
            parts = [p for p in parts if p]
            if not parts:
                continue
            front = parts[0][:240]
            back = "\n".join(parts[1:])[:1200]
            clean_tags = " ".join(str(tags or "").strip().split())
            body = f"Deck: {deck}\nQ: {front}" + (f"\nA: {back}" if back else "") + (f"\nTags: {clean_tags}" if clean_tags else "")
            ids.append(item_id)
            texts.append(body)
            metadatas.append({"source": "anki", "deck": deck, "front": front})

        for start in range(0, len(ids), 64):
            store.add(
                config.ANKI_COLLECTION,
                ids[start : start + 64],
                texts[start : start + 64],
                metadatas[start : start + 64],
            )
        store.delete_stale(config.ANKI_COLLECTION, set(seen.keys()))
        manifest["anki"] = seen
        return len(ids)
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


# ---------------------------------------------------------------- 3. PubMed + Zotero (.bib / .ris)


def _parse_bibtex_or_ris(path: Path) -> list[tuple[str, str, dict]]:
    """Offline parser for Zotero/Mendeley .bib and .ris bibliography exports."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    items: list[tuple[str, str, dict]] = []
    if path.suffix.lower() == ".ris":
        for entry in re.split(r"\nER\s+-\s*", raw):
            fields: dict[str, list[str]] = {}
            for line in entry.splitlines():
                m = re.match(r"^([A-Z0-9]{2})\s+-\s+(.*)$", line.strip())
                if m:
                    fields.setdefault(m.group(1), []).append(m.group(2).strip())
            title = " ".join(fields.get("TI") or fields.get("T1") or ())
            abstract = " ".join(fields.get("AB") or fields.get("N2") or ())
            if not title:
                continue
            year = (fields.get("PY") or fields.get("Y1") or [""])[0][:4]
            authors = ", ".join((fields.get("AU") or ())[:4])
            doi = (fields.get("DO") or [""])[0]
            digest = hashlib.sha1(title.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
            url = f"https://doi.org/{doi}" if doi and not doi.startswith("http") else (doi if doi.startswith("https://") else "")
            body = f"{title}" + (f" ({year})" if year else "") + (f"\nAuthors: {authors}" if authors else "") + (f"\n\n{abstract}" if abstract else "")
            meta = {"source": "pubmed", "key": f"Ref:{year or digest[:4]}", "title": title}
            if url.startswith("https://"):
                meta["url"] = url
            items.append((f"bib:{digest}", body[:3500], meta))
        return items

    # BibTeX (@article{key, ...})
    for match in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,([^@]+)", raw, re.DOTALL):
        cite_key = match.group(1).strip()
        block = match.group(2)

        def field_val(name: str, b: str = block) -> str:
            m = re.search(rf"\b{name}\s*=\s*(?:\{{(.*?)\}}|\"(.*?)\")", b, re.IGNORECASE | re.DOTALL)
            val = (m.group(1) or m.group(2) or "") if m else ""
            return re.sub(r"\s+", " ", val.replace("{", "").replace("}", "")).strip()

        title = field_val("title")
        if not title:
            continue
        abstract = field_val("abstract")
        author = field_val("author")
        year = field_val("year")
        pmid = field_val("pmid")
        doi = field_val("doi")
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (f"https://doi.org/{doi}" if doi else "")
        body = f"{title}" + (f" ({year})" if year else "") + (f"\nAuthors: {author[:180]}" if author else "") + (f"\n\n{abstract}" if abstract else "")
        meta: dict = {"source": "pubmed", "key": cite_key, "title": title}
        if pmid:
            meta["pmid"] = pmid
        if url.startswith("https://"):
            meta["url"] = url
        items.append((f"bib:{cite_key}", body[:3500], meta))
    return items


def _fetch_pubmed_articles(query: str, max_results: int = 40) -> list[tuple[str, str, dict]]:
    _require_network()
    api_key = os.environ.get("NCBI_API_KEY", "").strip()
    key_param = f"&api_key={urllib.parse.quote(api_key)}" if api_key else ""

    # Support either comma-separated PMIDs or a free-text PubMed search query.
    pmid_list = re.findall(r"\b\d{6,10}\b", query)
    if not pmid_list or len(pmid_list) < len([p for p in query.split(",") if p.strip()]):
        term = urllib.parse.quote(query.strip())
        search_url = (
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=pubmed&retmode=json&sort=relevance&retmax={max_results}&term={term}{key_param}"
        )
        req = urllib.request.Request(  # noqa: S310
            search_url, headers={"User-Agent": "Clueless/0.2 (local-assistant)"}
        )
        with urllib.request.urlopen(req, timeout=30, context=tls.ssl_context()) as resp:  # noqa: S310  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
        pmid_list = (data.get("esearchresult") or {}).get("idlist") or []

    if not pmid_list:
        return []

    ids_csv = ",".join(pmid_list[:max_results])
    fetch_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&retmode=xml&id={ids_csv}{key_param}"
    )
    req = urllib.request.Request(  # noqa: S310
        fetch_url, headers={"User-Agent": "Clueless/0.2 (local-assistant)"}
    )
    with urllib.request.urlopen(req, timeout=45, context=tls.ssl_context()) as resp:  # noqa: S310  # nosec B310
        xml_bytes = resp.read()

    root = _safe_xml_root(xml_bytes)
    if root is None:
        return []

    articles: list[tuple[str, str, dict]] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = (article.findtext(".//MedlineCitation/PMID") or "").strip()
        title = "".join(article.find(".//ArticleTitle").itertext()).strip() if article.find(".//ArticleTitle") is not None else ""
        if not pmid or not title:
            continue
        journal = (article.findtext(".//Journal/Title") or "").strip()
        year = (article.findtext(".//JournalIssue/PubDate/Year") or "").strip()
        abstract_parts = []
        for node in article.findall(".//Abstract/AbstractText"):
            label = node.attrib.get("Label", "").strip()
            txt = "".join(node.itertext()).strip()
            if txt:
                abstract_parts.append(f"{label}: {txt}" if label else txt)
        if not abstract_parts:
            continue
        body = (
            f"PMID:{pmid} — {title}\n"
            + (f"Journal: {journal} ({year})\n\n" if journal else "\n")
            + "\n".join(abstract_parts)
        )
        articles.append((
            f"pmid:{pmid}",
            body[:4000],
            {
                "source": "pubmed",
                "pmid": pmid,
                "key": f"PMID:{pmid}",
                "title": title,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            },
        ))
    return articles


def index_pubmed(store: rag.Store, manifest: dict, incremental: bool = True) -> int:
    """Indexes offline Zotero bibliographies and/or PubMed abstracts."""
    all_items: list[tuple[str, str, dict]] = []

    if config.ZOTERO_BIB_PATH:
        bib = Path(config.ZOTERO_BIB_PATH).expanduser()
        if bib.is_file():
            all_items.extend(_parse_bibtex_or_ris(bib))

    if config.PUBMED_QUERY and config.NETWORK_INDEXING:
        try:
            all_items.extend(_fetch_pubmed_articles(config.PUBMED_QUERY))
        except Exception as error:
            log.warning("pubmed fetch failed: %s", error)

    if not all_items:
        return 0

    prev: dict[str, str] = manifest.get("pubmed", {})
    seen: dict[str, str] = {}
    ids, texts, metadatas = [], [], []
    for item_id, body, meta in all_items:
        fp = hashlib.sha1(body.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        seen[item_id] = fp
        if incremental and prev.get(item_id) == fp:
            continue
        ids.append(item_id)
        texts.append(body)
        metadatas.append(meta)

    for start in range(0, len(ids), 64):
        store.add(
            config.PUBMED_COLLECTION,
            ids[start : start + 64],
            texts[start : start + 64],
            metadatas[start : start + 64],
        )
    store.delete_stale(config.PUBMED_COLLECTION, set(seen.keys()))
    manifest["pubmed"] = seen
    return len(ids)


# ---------------------------------------------------------------- 4. Canvas LMS


def _canvas_get(base_url: str, path: str, token: str) -> list | dict:
    _require_network()
    if not base_url.startswith("https://"):
        raise ValueError("Canvas URL must use https://")
    req = urllib.request.Request(  # noqa: S310
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30, context=tls.ssl_context()) as resp:  # noqa: S310  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def index_canvas(store: rag.Store, manifest: dict, incremental: bool = True) -> int:
    """Indexes active Canvas LMS courses, syllabi, pages and assignments."""
    token = os.environ.get("CANVAS_TOKEN", "").strip()
    if not config.CANVAS_URL or not token:
        return 0
    _require_network()

    try:
        courses = _canvas_get(
            config.CANVAS_URL,
            "/api/v1/courses?enrollment_state=active&include[]=syllabus_body&per_page=20",
            token,
        )
    except Exception as error:
        log.warning("canvas courses fetch failed: %s", error)
        return 0

    prev: dict[str, str] = manifest.get("canvas", {})
    seen: dict[str, str] = {}
    ids, texts, metadatas = [], [], []

    for course in courses if isinstance(courses, list) else []:
        cid = course.get("id")
        cname = str(course.get("course_code") or course.get("name") or f"Course {cid}")
        if not cid:
            continue

        syllabus = strip_html(str(course.get("syllabus_body") or ""))
        if len(syllabus) >= 60:
            for idx, piece in enumerate(_chunk_text_windows(syllabus)):
                iid = f"canvas:{cid}:syllabus:{idx}"
                fp = hashlib.sha1(piece.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
                seen[iid] = fp
                if not (incremental and prev.get(iid) == fp):
                    ids.append(iid)
                    texts.append(f"{cname} — Syllabus\n{piece}")
                    metadatas.append({
                        "source": "canvas", "course": cname, "title": "Syllabus",
                        "url": f"{config.CANVAS_URL}/courses/{cid}/assignments/syllabus",
                    })

        # Assignments (due dates, prompts & rubrics)
        try:
            assignments = _canvas_get(config.CANVAS_URL, f"/api/v1/courses/{cid}/assignments?per_page=35", token)
        except Exception:
            assignments = []
        for item in assignments if isinstance(assignments, list) else []:
            aid = item.get("id")
            title = str(item.get("name") or "")
            updated = str(item.get("updated_at") or "")
            if not aid or not title:
                continue
            iid = f"canvas:{cid}:assign:{aid}"
            seen[iid] = updated
            if incremental and updated and prev.get(iid) == updated:
                continue
            due = str(item.get("due_at") or "No due date")
            desc = strip_html(str(item.get("description") or ""))[:1800]
            ids.append(iid)
            texts.append(f"{cname} — Assignment: {title}\nDue: {due}\n\n{desc}".strip())
            metadatas.append({
                "source": "canvas", "course": cname, "title": title,
                "url": str(item.get("html_url") or f"{config.CANVAS_URL}/courses/{cid}/assignments/{aid}"),
            })

        # Course wiki pages
        try:
            pages = _canvas_get(config.CANVAS_URL, f"/api/v1/courses/{cid}/pages?per_page=30", token)
        except Exception:
            pages = []
        for page_summary in pages if isinstance(pages, list) else []:
            slug = page_summary.get("url")
            title = str(page_summary.get("title") or "")
            updated = str(page_summary.get("updated_at") or "")
            if not slug or not title:
                continue
            iid = f"canvas:{cid}:page:{slug}"
            seen[iid] = updated
            if incremental and updated and prev.get(iid) == updated:
                continue
            try:
                detail = _canvas_get(config.CANVAS_URL, f"/api/v1/courses/{cid}/pages/{urllib.parse.quote(str(slug))}", token)
                body = strip_html(str((detail or {}).get("body") or ""))[:2200]
            except Exception as err:
                log.debug("canvas page %s skipped: %s", slug, err)
                continue
            if not body:
                continue
            ids.append(iid)
            texts.append(f"{cname} — {title}\n\n{body}")
            metadatas.append({
                "source": "canvas", "course": cname, "title": title,
                "url": f"{config.CANVAS_URL}/courses/{cid}/pages/{slug}",
            })

    for start in range(0, len(ids), 64):
        store.add(
            config.CANVAS_COLLECTION,
            ids[start : start + 64],
            texts[start : start + 64],
            metadatas[start : start + 64],
        )
    store.delete_stale(config.CANVAS_COLLECTION, set(seen.keys()))
    manifest["canvas"] = seen
    return len(ids)


# ---------------------------------------------------------------- 5. Notion


def _notion_request(path: str, token: str, payload: dict | None = None) -> dict:
    _require_network()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"https://api.notion.com{path}",
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30, context=tls.ssl_context()) as resp:  # noqa: S310  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _notion_title(page: dict) -> str:
    props = page.get("properties") or {}
    for prop in props.values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in (prop.get("title") or [])).strip()
    return "Untitled"


def index_notion(store: rag.Store, manifest: dict, incremental: bool = True) -> int:
    """Indexes Notion pages shared with the user's integration token."""
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        return 0
    _require_network()

    search_body: dict = {"filter": {"value": "page", "property": "object"}, "page_size": 50}
    if config.NOTION_QUERY:
        search_body["query"] = config.NOTION_QUERY

    try:
        data = _notion_request("/v1/search", token, search_body)
    except Exception as error:
        log.warning("notion search failed: %s", error)
        return 0

    prev: dict[str, str] = manifest.get("notion", {})
    seen: dict[str, str] = {}
    ids, texts, metadatas = [], [], []

    for page in data.get("results") or []:
        page_id = str(page.get("id") or "")
        edited = str(page.get("last_edited_time") or "")
        if not page_id:
            continue
        title = _notion_title(page)
        iid = f"notion:{page_id}"
        seen[iid] = edited
        if incremental and edited and prev.get(iid) == edited:
            continue

        try:
            blocks = _notion_request(f"/v1/blocks/{page_id}/children?page_size=100", token)
        except Exception as err:
            log.debug("notion page %s blocks skipped: %s", page_id, err)
            continue

        lines = []
        for block in blocks.get("results") or []:
            btype = block.get("type", "")
            content = block.get(btype) or {}
            rich = content.get("rich_text") or []
            plain = "".join(r.get("plain_text", "") for r in rich).strip()
            if plain:
                lines.append(plain)
        body = "\n".join(lines).strip()
        if not body:
            continue

        url = str(page.get("url") or "")
        ids.append(iid)
        texts.append(f"{title}\n\n{body[:2800]}")
        meta = {"source": "notion", "title": title}
        if url.startswith("https://"):
            meta["url"] = url
        metadatas.append(meta)

    for start in range(0, len(ids), 64):
        store.add(
            config.NOTION_COLLECTION,
            ids[start : start + 64],
            texts[start : start + 64],
            metadatas[start : start + 64],
        )
    store.delete_stale(config.NOTION_COLLECTION, set(seen.keys()))
    manifest["notion"] = seen
    return len(ids)


# ---------------------------------------------------------------- 6. Live macOS Calendar (EventKit)

_calendar_cache: dict = {"ts": 0.0, "text": "", "attendees": [], "status": "unknown"}


def calendar_status(request_access: bool = False) -> dict:
    """Checks or requests macOS Calendar permission via native/build/calendar."""
    if not config.CALENDAR_BINARY.exists():
        return {"status": "unavailable", "authorized": False, "events": []}
    args = [str(config.CALENDAR_BINARY)]
    args.append("--request" if request_access else "--check")
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=30)
        data = json.loads(res.stdout.strip().splitlines()[-1])
        return data
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return {"status": "error", "authorized": False, "events": []}


def get_calendar_context(force: bool = False) -> tuple[str, list[str]]:
    """Returns (formatted_event_summary, attendee_names) for active/upcoming meetings."""
    if config.CLINICAL_MODE or not config.CALENDAR_ENABLED or not config.CALENDAR_BINARY.exists():
        return "", []

    now = time.monotonic()
    if not force and _calendar_cache["ts"] and now - _calendar_cache["ts"] < 180:
        return _calendar_cache["text"], list(_calendar_cache["attendees"])

    try:
        res = subprocess.run(
            [str(config.CALENDAR_BINARY), "--window-minutes", "90"],
            capture_output=True, text=True, timeout=10,
        )
        payload = json.loads(res.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return "", []

    events = payload.get("events") or []
    lines: list[str] = []
    attendees: list[str] = []
    for ev in events[:3]:
        tag = "[NOW] " if ev.get("current") else "[UPCOMING] "
        ev_attendees = [str(a) for a in (ev.get("attendees") or []) if a]
        attendees.extend(ev_attendees)
        people = f" | Attendees: {', '.join(ev_attendees[:12])}" if ev_attendees else ""
        notes = f"\nAgenda: {ev['notes']}" if ev.get("notes") else ""
        lines.append(f"{tag}{ev.get('title', 'Meeting')}{people}{notes}")

    text = "\n\n".join(lines)
    _calendar_cache.update({
        "ts": now,
        "text": text,
        "attendees": list(dict.fromkeys(attendees)),
        "status": payload.get("status", "unknown"),
    })
    return text, list(_calendar_cache["attendees"])
