# Tests always use a throwaway SQLite file, never the DATABASE_URL in your shell (it could be Neon).
import os as _os
import tempfile as _tempfile

_os.environ.pop("DATABASE_URL", None)
_os.environ["PAPERS_DB"] = _os.path.join(_tempfile.mkdtemp(), "papers.sqlite3")

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import pymupdf

from backend import main
from backend.database import create_database_engine, database_connection
from backend.migrate_to_postgres import migrate
from backend.main import CATEGORIES, DocumentedAffiliation, ReviewUpdate, normalize_arxiv_id, parse_feed


class PaperPipelineTests(unittest.TestCase):
    def test_parse_oai_record_preserves_paper_metadata_without_inferred_affiliations(self):
        feed = b"""<?xml version="1.0"?>
        <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"
          xmlns:arxiv="http://arxiv.org/OAI/arXiv/">
          <ListRecords>
            <record>
              <header><identifier>oai:arXiv.org:2401.01234</identifier></header>
              <metadata><arxiv:arXiv>
                <arxiv:id>2401.01234</arxiv:id>
                <arxiv:created>2024-01-03</arxiv:created>
                <arxiv:title> A test paper </arxiv:title>
                <arxiv:abstract>A test abstract.</arxiv:abstract>
                <arxiv:authors><arxiv:author>
                  <arxiv:forenames>Example</arxiv:forenames>
                  <arxiv:keyname>Author</arxiv:keyname>
                </arxiv:author></arxiv:authors>
                <arxiv:categories>cs.AI stat.ML</arxiv:categories>
              </arxiv:arXiv></metadata>
            </record>
            <resumptionToken cursor="0" completeListSize="1">verb%3DListRecords%26skip%3D1300</resumptionToken>
          </ListRecords>
        </OAI-PMH>"""

        papers, token, total = parse_feed(feed)
        paper = papers[0]

        self.assertEqual(paper["arxiv_id"], "2401.01234")
        self.assertEqual(paper["title"], "A test paper")
        self.assertEqual(paper["authors"], ["Example Author"])
        self.assertEqual(paper["primary_category"], "cs.AI")
        self.assertEqual(paper["published_at"], "2024-01-03T00:00:00Z")
        self.assertIsNone(paper["institution"])
        self.assertIsNone(paper["country"])
        self.assertEqual(token, "verb=ListRecords&skip=1300")
        self.assertEqual(total, 1)

    def test_archive_topics_cover_the_full_arxiv_cs_taxonomy(self):
        self.assertEqual(len(CATEGORIES), 40)
        self.assertEqual(CATEGORIES[0], ("cs.AI", "Artificial Intelligence"))
        self.assertEqual(CATEGORIES[-1], ("cs.SY", "Systems and Control"))

    def test_version_suffix_is_removed_from_arxiv_ids(self):
        self.assertEqual(normalize_arxiv_id("https://arxiv.org/abs/2401.01234v7"), "2401.01234")

    def test_pdf_affiliation_suggestions_require_matching_documented_quotes(self):
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "Example University, United States")
        pdf_bytes = document.tobytes()
        document.close()
        provider_reply = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "candidates": [
                            {
                                "institution": "Example University",
                                "country": "United States",
                                "evidence_quote": "Example University, United States",
                            },
                            {
                                "institution": "Unlisted Institute",
                                "country": "Elsewhere",
                                "evidence_quote": "Example University, United States",
                            },
                        ]
                    })
                }
            }]
        }
        with (
            patch.dict(os.environ, {"AI_CHAT_MODEL": "test-model"}),
            patch("backend.ai_service.urllib.request.urlopen") as mocked_urlopen,
            patch("backend.ai_service.ai_request", return_value=provider_reply) as mocked_ai,
        ):
            mocked_urlopen.return_value.__enter__.return_value.read.return_value = pdf_bytes
            result = main.extract_pdf_affiliations("2401.01234", "A paper")
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertEqual(result["suggestions"][0]["country"], "United States")
        self.assertEqual(result["suggestions"][0]["source_url"], "https://arxiv.org/pdf/2401.01234")
        prompt = mocked_ai.call_args.args[1]["messages"][0]["content"]
        self.assertIn("Do not infer an affiliation from an author name", prompt)

    def test_ai_embeddings_and_summaries_validate_openai_compatible_responses(self):
        with patch.dict(os.environ, {"AI_EMBEDDING_MODEL": "embed-test", "AI_CHAT_MODEL": "chat-test"}):
            with patch(
                "backend.ai_service.ai_request",
                return_value={"data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]},
            ):
                vectors = main.generate_embeddings(["first", "second"])
            summary_fields = {
                "summary": "Plain-language summary.",
                "key_contribution": "Main contribution.",
                "application": "Example application.",
                "relevance": "Why this was surfaced.",
                "topics": "Research topic",
                "methods": "Method",
                "context": "Application area",
            }
            with patch(
                "backend.ai_service.ai_request",
                return_value={"choices": [{"message": {"content": json.dumps(summary_fields)}}]},
            ):
                summary = main.generate_research_analysis({"title": "Title", "abstract": "Abstract"})
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(summary, summary_fields)


class DatabaseAdapterTests(unittest.TestCase):
    def test_portable_connection_binds_parameters_and_reads_named_or_positional_columns(self):
        engine = create_database_engine("sqlite://")
        try:
            with database_connection(engine) as connection:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
                inserted = connection.execute(
                    "INSERT INTO sample (id, name) VALUES (?, ?)",
                    (1, "Portable row"),
                )
                row = connection.execute("SELECT id, name FROM sample").fetchone()
            self.assertEqual(inserted.rowcount, 1)
            self.assertEqual(row[0], 1)
            self.assertEqual(row["name"], "Portable row")
        finally:
            engine.dispose()

    def test_legacy_category_backfill_uses_database_independent_json_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = create_database_engine(f"sqlite:///{Path(temp) / 'legacy.sqlite3'}")
            legacy_file = Path(temp) / "legacy.json"
            legacy_file.write_text(json.dumps([{
                "raw_metadata": {
                    "arxiv_id": "2401.98765",
                    "categories": ["cs.AI", "cs.CL", "math.AG"],
                    "title": "Legacy paper",
                    "abstract": "Abstract",
                    "authors": [],
                    "published_at": "2024-01-01",
                },
                "analysis": {},
            }]), encoding="utf-8")
            previous_engine, previous_legacy_file = main.ENGINE, main.LEGACY_FILE
            main.ENGINE, main.LEGACY_FILE = engine, legacy_file
            try:
                main.init_db()
                with main.connect_db() as connection:
                    categories = connection.execute(
                        "SELECT category FROM paper_categories ORDER BY category"
                    ).fetchall()
                self.assertEqual([row[0] for row in categories], ["cs.AI", "cs.CL"])
            finally:
                engine.dispose()
                main.ENGINE, main.LEGACY_FILE = previous_engine, previous_legacy_file

    def test_sqlite_migration_is_idempotent_and_preserves_categories_and_ingest_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            source_path = Path(temp) / "source.sqlite3"
            target_engine = create_database_engine(f"sqlite:///{Path(temp) / 'target.sqlite3'}")
            source = sqlite3.connect(source_path)
            try:
                source.executescript(
                    """
                    CREATE TABLE papers (
                        arxiv_id TEXT PRIMARY KEY, raw_metadata TEXT NOT NULL, analysis TEXT NOT NULL,
                        primary_category TEXT NOT NULL, title TEXT NOT NULL, abstract TEXT NOT NULL,
                        authors TEXT NOT NULL, institution TEXT, country TEXT, published_at TEXT NOT NULL,
                        approval_status TEXT NOT NULL
                    );
                    CREATE TABLE paper_categories (arxiv_id TEXT NOT NULL, category TEXT NOT NULL);
                    CREATE TABLE ingest_state (id INTEGER PRIMARY KEY, state_json TEXT NOT NULL);
                    """
                )
                raw = {"arxiv_id": "2401.12345", "categories": ["cs.AI", "cs.CL"]}
                source.execute(
                    "INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("2401.12345", json.dumps(raw), "{}", "cs.AI", "Title", "Abstract", "[]", None, None, "2024-01-01", "pending"),
                )
                source.executemany(
                    "INSERT INTO paper_categories VALUES (?, ?)",
                    [("2401.12345", "cs.AI"), ("2401.12345", "cs.CL")],
                )
                source.execute("INSERT INTO ingest_state VALUES (1, ?)", ('{"status":"error","resumption_token":"cursor"}',))
                source.commit()
            finally:
                source.close()

            previous_engine = main.ENGINE
            previous_legacy = main.LEGACY_FILE
            main.ENGINE = target_engine
            main.LEGACY_FILE = Path(temp) / "missing-legacy.json"
            try:
                main.init_db()
                first = migrate(source_path)
                second = migrate(source_path)
                with main.connect_db() as target:
                    paper_count = target.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
                    categories = target.execute("SELECT category FROM paper_categories ORDER BY category").fetchall()
                    state = json.loads(target.execute("SELECT state_json FROM ingest_state WHERE id = 1").fetchone()[0])
                self.assertEqual(first["target_papers_after_migration"], 1)
                self.assertEqual(second["target_papers_after_migration"], 1)
                self.assertEqual(paper_count, 1)
                self.assertEqual([row[0] for row in categories], ["cs.AI", "cs.CL"])
                self.assertEqual(state["resumption_token"], "cursor")
            finally:
                target_engine.dispose()
                main.ENGINE = previous_engine
                main.LEGACY_FILE = previous_legacy


class ReviewWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.original_engine = main.ENGINE
        cls.original_legacy_file = main.LEGACY_FILE
        main.ENGINE = create_database_engine(
            f"sqlite:///{Path(cls.temporary_directory.name) / 'reviews.sqlite3'}"
        )
        main.LEGACY_FILE = Path(cls.temporary_directory.name) / "missing-legacy-data.json"
        main.init_db()
        main.insert_papers([
            {
                "arxiv_id": "2601.00001",
                "url": "https://arxiv.org/abs/2601.00001",
                "title": "Graph Neural Networks for Medical Images",
                "abstract": "We study graph neural networks for medical image classification.",
                "authors": ["A One"],
                "published_at": "2026-01-01T00:00:00Z",
                "categories": ["cs.AI", "cs.CV"],
                "primary_category": "cs.AI",
            },
            {
                "arxiv_id": "2601.00002",
                "url": "https://arxiv.org/abs/2601.00002",
                "title": "Language Models for Clinical Support",
                "abstract": "This work studies language models for clinical decision support.",
                "authors": ["B Two"],
                "published_at": "2026-01-02T00:00:00Z",
                "categories": ["cs.AI", "cs.CL"],
                "primary_category": "cs.AI",
            },
        ])

    @classmethod
    def tearDownClass(cls):
        main.ENGINE.dispose()
        main.ENGINE = cls.original_engine
        main.LEGACY_FILE = cls.original_legacy_file
        cls.temporary_directory.cleanup()

    def setUp(self):
        self.admin_token_patcher = patch.dict(os.environ, {"ADMIN_API_TOKEN": "test-token"})
        self.admin_token_patcher.start()
        with main.connect_db() as connection:
            connection.execute("DELETE FROM paper_categories")
            connection.execute("DELETE FROM papers")
        main.insert_papers([
            {
                "arxiv_id": "2601.00001",
                "url": "https://arxiv.org/abs/2601.00001",
                "title": "Graph Neural Networks for Medical Images",
                "abstract": "We study graph neural networks for medical image classification.",
                "authors": ["A One"],
                "published_at": "2026-01-01T00:00:00Z",
                "categories": ["cs.AI", "cs.CV"],
                "primary_category": "cs.AI",
            },
            {
                "arxiv_id": "2601.00002",
                "url": "https://arxiv.org/abs/2601.00002",
                "title": "Language Models for Clinical Support",
                "abstract": "This work studies language models for clinical decision support.",
                "authors": ["B Two"],
                "published_at": "2026-01-02T00:00:00Z",
                "categories": ["cs.AI", "cs.CL"],
                "primary_category": "cs.AI",
            },
        ])

    def tearDown(self):
        self.admin_token_patcher.stop()

    def test_only_approved_papers_are_returned_in_the_public_featured_collection(self):
        main.update_paper_review(
            "2601.00001",
            ReviewUpdate(approval_status="approved", summary="A reviewed plain-language summary."),
            "Bearer test-token",
        )
        result = main.list_featured_papers(
            limit=12,
            offset=0,
            q="",
            category=None,
            country=None,
            institution=None,
            year=None,
            sort="newest",
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["papers"][0]["arxiv_id"], "2601.00001")
        self.assertEqual(result["papers"][0]["summary"], "A reviewed plain-language summary.")

    def test_auto_publish_mode_features_approved_first_and_hides_rejected(self):
        main.update_paper_review("2601.00001", ReviewUpdate(approval_status="approved"), "Bearer test-token")
        arguments = dict(limit=12, offset=0, q="", category=None, country=None, institution=None, year=None, sort="newest")
        with patch.object(main, "PUBLISH_MODE", "auto"):
            result = main.list_featured_papers(**arguments)
            self.assertEqual(result["publish_mode"], "auto")
            self.assertEqual([paper["arxiv_id"] for paper in result["papers"]], ["2601.00001", "2601.00002"])
            main.update_paper_review("2601.00002", ReviewUpdate(approval_status="rejected"), "Bearer test-token")
            self.assertEqual(main.list_featured_papers(**arguments)["total"], 1)

    def test_daily_update_imports_new_papers_and_remembers_the_run_date(self):
        from backend import update_papers

        new_paper = {
            "arxiv_id": "2611.00009", "url": "https://arxiv.org/abs/2611.00009", "title": "A brand new paper",
            "abstract": "Fresh results.", "authors": ["N New"], "published_at": "2026-11-01T00:00:00Z",
            "categories": ["cs.DS"], "primary_category": "cs.DS",
        }
        calls = []

        def fake_fetch(token, from_date=None):
            calls.append((token, from_date))
            return [new_paper], None, 1

        with patch.object(update_papers, "fetch_oai_page", fake_fetch), \
                patch.object(update_papers.affiliation_scan, "fetch_openalex", return_value={}), \
                patch("sys.argv", ["update_papers", "--since", "2026-10-31"]), \
                patch("builtins.print"):
            update_papers.main()
        self.assertEqual(calls, [(None, "2026-10-31")])
        with main.connect_db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM papers WHERE arxiv_id = '2611.00009'").fetchone()[0], 1
            )
        state = update_papers.read_update_state()
        self.assertEqual(state["last_result"]["new_papers"], 1)
        self.assertIn("last_run_date", state)

    def test_topic_links_are_weighted_by_shared_paper_count(self):
        main.insert_papers([{
            "arxiv_id": "2601.00003",
            "url": "https://arxiv.org/abs/2601.00003",
            "title": "Another computer vision paper",
            "abstract": "A paper shared by artificial intelligence and computer vision.",
            "authors": ["C Three"],
            "published_at": "2026-01-03T00:00:00Z",
            "categories": ["cs.AI", "cs.CV"],
            "primary_category": "cs.AI",
        }])
        result = main.list_topic_links()
        links = {(link["source"], link["target"]): link["shared_papers"] for link in result["links"]}
        self.assertEqual(links[("cs.AI", "cs.CV")], 2)
        self.assertEqual(links[("cs.AI", "cs.CL")], 1)
        self.assertEqual(result["available_links"], 2)

    def test_admin_review_requires_configured_valid_token(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "test-token"}):
            with self.assertRaises(HTTPException) as error:
                main.admin_overview("Bearer wrong-token")
            self.assertEqual(error.exception.status_code, 401)
            overview = main.admin_overview("Bearer test-token")
        self.assertEqual(overview["pending"], 2)

    def test_ingestion_mutations_require_admin_authorization(self):
        actions = (
            lambda: main.ingest_sample(authorization="Bearer invalid-token"),
            lambda: main.start_full_ingest("Bearer invalid-token"),
            lambda: main.pause_full_ingest("Bearer invalid-token"),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(action())
                self.assertEqual(error.exception.status_code, 401)

    def test_affiliation_requires_cited_evidence_and_updates_metadata_filters(self):
        main.update_documented_affiliation(
            "2601.00001",
            DocumentedAffiliation(
                institution="Example University",
                country="Example Country",
                source_url="https://example.edu/research/paper",
                evidence="The first-page author affiliation lists Example University.",
            ),
            "Bearer test-token",
        )
        result = main.list_papers(
            limit=10,
            offset=0,
            q="",
            category=None,
            country=None,
            institution=None,
            year=None,
            sort="newest",
        )
        enriched = next(paper for paper in result["papers"] if paper["arxiv_id"] == "2601.00001")
        self.assertEqual(enriched["institution"], "Example University")
        self.assertEqual(enriched["country"], "Example Country")
        discovery = enriched["discovery"]
        self.assertEqual(set(discovery["factors"]), {"topic", "institution", "geography"})
        self.assertEqual(discovery["weights"], {"topic": 0.4, "institution": 0.3, "geography": 0.3})
        self.assertEqual(discovery["measures"], "representation")
        self.assertEqual(main.get_filter_options()["countries"], ["Example Country"])


if __name__ == "__main__":
    unittest.main()
