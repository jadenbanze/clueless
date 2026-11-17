"""Offline unit tests for neutral context providers, Jira/Confluence formatting,
Clinical-mode network blocking, and write-only .env credential persistence.

Run: python3 -m server.test_context_sources
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

from . import api, config, context_sources, indexers, providers, rag
from .providers.base import ProviderError

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def _make_sample_pptx(path: Path) -> None:
    slide_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<p:cSld><p:spTree><p:sp><p:txBody><a:p>"
        "<a:r><a:t>Frank-Starling Mechanism: stroke volume rises with preload.</a:t></a:r>"
        "</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide_xml)


def _make_sample_docx(path: Path) -> None:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Syllabus: Problem-based learning meets Tuesdays at 10am.</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)


def _make_sample_apkg(path: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp_db:
        db_file = Path(tmp_db.name)
    try:
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO decks VALUES (1, 'Cardiology::Pharmacology')")
            conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, mod INTEGER, flds TEXT, tags TEXT)")
            conn.execute(
                "INSERT INTO notes VALUES (101, 1700000000, ?, 'step1 cardio')",
                ("What is the mechanism of {{c1::Furosemide::drug}}?\x1fInhibits NKCC2 in thick ascending limb.",),
            )
            conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER)")
            conn.execute("INSERT INTO cards VALUES (201, 101, 1)")
            conn.commit()
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("collection.anki21", db_file.read_bytes())
    finally:
        db_file.unlink(missing_ok=True)


def main() -> int:
    saved_env = os.environ.get("CLUELESS_DATA_DIR")
    saved_embed = providers.embed

    def bm25_only_embed(_texts):
        raise ProviderError("offline test: BM25 mode")

    providers.embed = bm25_only_embed

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        notes_dir = data_dir / "study_vault"
        notes_dir.mkdir()
        (notes_dir / "lecture1.md").write_text(
            "# Renal Physiology\n[[Glomerular filtration rate|GFR]] is regulated by afferent arteriole tone.\n"
        )
        _make_sample_pptx(notes_dir / "cardio-slides.pptx")
        _make_sample_docx(notes_dir / "syllabus.docx")

        apkg_path = data_dir / "deck.apkg"
        _make_sample_apkg(apkg_path)

        bib_path = data_dir / "refs.bib"
        bib_path.write_text(
            "@article{smith2025sepsis,\n"
            "  title = {Early Balanced Crystalloids in Sepsis},\n"
            "  author = {Smith, Jane and Doe, Alex},\n"
            "  year = {2025},\n"
            "  pmid = {39911223},\n"
            "  abstract = {Balanced crystalloids reduced major adverse kidney events compared with saline.}\n"
            "}\n"
        )

        (data_dir / ".cache").mkdir()
        (data_dir / ".cache" / "settings.json").write_text(json.dumps({
            "PROFILE": "student",
            "CLINICAL_MODE": "0",
            "LOCAL_NOTES_DIRS": str(notes_dir),
            "ANKI_PATH": str(apkg_path),
            "ZOTERO_BIB_PATH": str(bib_path),
        }))
        os.environ["CLUELESS_DATA_DIR"] = str(data_dir)
        config.reload()
        rag._embed_cache.clear()

        try:
            print("local files (.md, .pptx, .docx), Anki (.apkg), and Zotero (.bib)")
            store = rag.Store()
            res = indexers.build_all(store=store)
            check("indexed markdown + pptx + docx chunks", int(res.get("notes", 0)) >= 3, str(res))
            check("indexed Anki .apkg card", int(res.get("anki", 0)) == 1, str(res))
            check("indexed Zotero .bib paper", int(res.get("pubmed", 0)) == 1, str(res))

            pptx_hits = store.search_all("Frank-Starling preload stroke volume", sources={"notes"})
            check("retrieves PPTX slide with slide number citation",
                  bool(pptx_hits) and "slide 1" in pptx_hits[0].citation(),
                  pptx_hits[0].citation() if pptx_hits else "none")

            anki_hits = store.search_all("Furosemide NKCC2 mechanism", sources={"anki"})
            check("strips Anki cloze markup cleanly",
                  bool(anki_hits) and "Furosemide" in anki_hits[0].text and "{{c1" not in anki_hits[0].text)

            bib_hits = store.search_all("balanced crystalloids sepsis kidney", sources={"pubmed"})
            check("retrieves BibTeX paper with PubMed URL",
                  bool(bib_hits) and bib_hits[0].citation_meta().get("url") == "https://pubmed.ncbi.nlm.nih.gov/39911223/")

            print("Jira & Confluence enrichment helpers")
            _id, jira_body, jira_meta = indexers.format_jira_issue({
                "key": "PROJ-42",
                "fields": {
                    "summary": "Fix token refresh race",
                    "status": {"name": "In Review"},
                    "priority": {"name": "High"},
                    "assignee": {"displayName": "Alex Rivera"},
                    "issuetype": {"name": "Bug"},
                    "parent": {"key": "PROJ-1", "fields": {"summary": "Auth Epic"}},
                    "issuelinks": [{"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "PROJ-39"}}],
                    "comment": {"comments": [{"author": {"displayName": "Sam"}, "body": "PR #88 opened."}]},
                },
            }, site="example.atlassian.net")
            check("Jira includes parent epic, blocker link, and comments",
                  "PROJ-1" in jira_body and "is blocked by PROJ-39" in jira_body and "PR #88 opened" in jira_body)
            check("Jira attaches browse URL", jira_meta.get("url") == "https://example.atlassian.net/browse/PROJ-42")

            sections = indexers.html_sections("<h1>Architecture</h1><p>Overview text.</p><h2>Storage</h2><p>SQLite details.</p>")
            check("Confluence HTML splits by heading hierarchy",
                  len(sections) == 2 and sections[1][0] == "Storage")

            print("Clinical sub-mode network lock")
            (data_dir / ".cache" / "settings.json").write_text(json.dumps({
                "PROFILE": "student",
                "CLINICAL_MODE": "1",
                "LOCAL_NOTES_DIRS": str(notes_dir),
                "CANVAS_URL": "https://school.instructure.com",
            }))
            config.reload()
            check("Clinical mode disables network_indexing", config.NETWORK_INDEXING is False)
            try:
                context_sources._require_network()
                check("Clinical mode raises NetworkIndexingDisabled on network call", False)
            except context_sources.NetworkIndexingDisabled:
                check("Clinical mode raises NetworkIndexingDisabled on network call", True)

            clinical_res = indexers.build_all(store=store)
            check("Clinical mode skips network indexers while keeping offline notes",
                  "network_skipped" in clinical_res, str(clinical_res))

            print("write-only .env credentials")
            sample_creds = {k: v for k, v in [("CANVAS_TOKEN", "sample-tok"), ("NOTION_TOKEN", "")]}
            presence = api.update_env_credentials(sample_creds)
            env_mode = (data_dir / ".env").stat().st_mode & 0o777
            check("saves .env with 0600 permissions", env_mode == 0o600, oct(env_mode))
            check("reports presence booleans without leaking secret value",
                  presence.get("CANVAS_TOKEN") is True and presence.get("NOTION_TOKEN") is False)
            os.environ.pop("CANVAS_TOKEN", None)
        finally:
            providers.embed = saved_embed
            if saved_env is None:
                os.environ.pop("CLUELESS_DATA_DIR", None)
            else:
                os.environ["CLUELESS_DATA_DIR"] = saved_env
            config.reload()
            rag._embed_cache.clear()

    print(f"\n{'PASS' if not failures else f'FAIL ({len(failures)})'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
