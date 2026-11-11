# Never read .env.local / .env (they may hold the Neon connection strings) during tests.
import os as _os

_os.environ["RDN_SKIP_ENV_FILES"] = "1"
_os.environ.pop("DATABASE_URL", None)
_os.environ.pop("DATABASE_URL_UNPOOLED", None)

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import text

from backend import affiliation_scan
from backend.database import create_database_engine

WORK = {
    "id": "https://openalex.org/W1",
    "doi": "https://doi.org/10.48550/arxiv.2601.00001",
    "authorships": [
        {"raw_author_name": "A. Author", "raw_affiliation_strings": ["Dept. of CS, Virginia State University, Petersburg, VA"],
         "institutions": [{"display_name": "Virginia State University", "country_code": "US", "type": "education",
                           "ror": "https://ror.org/0"}]},
        {"raw_author_name": "B. Author", "raw_affiliation_strings": ["Google Research"],
         "institutions": [{"display_name": "Google (United States)", "country_code": "US", "type": "company", "ror": None}]},
    ],
}

PDF_TEXT = (
    "A Paper Title\nJane Doe1, John Roe2\n1Department of Computer Science, University of Lagos, Lagos, Nigeria\n"
    "2Independent Researcher\nCanada\njane@example.org\nAbstract\nWe study things at the University of Nowhere.\n"
)


class AffiliationScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_database_engine(f"sqlite:///{Path(self.tmp.name) / 'p.sqlite3'}")
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, analysis TEXT NOT NULL, title TEXT, institution TEXT, country TEXT, "
                "published_at TEXT)"
            ))
            for arxiv_id, institution in [("2601.00001", None), ("2601.00002", None), ("2601.00003", "Admin Entered Lab")]:
                connection.execute(text("INSERT INTO papers VALUES (:id, '{}', 't', :i, NULL, '2026-01-05')"), {"id": arxiv_id, "i": institution})

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def row(self, arxiv_id):
        with self.engine.connect() as connection:
            institution, country, analysis = connection.execute(
                text("SELECT institution, country, analysis FROM papers WHERE arxiv_id = :id"), {"id": arxiv_id}
            ).one()
        return institution, country, json.loads(analysis)

    def test_openalex_affiliations_are_parsed_with_evidence(self):
        items = affiliation_scan.affiliations_from_openalex(WORK)
        self.assertEqual(items[0]["institution"], "Virginia State University")
        self.assertEqual(items[0]["country"], "United States")
        self.assertIn("Virginia State University", items[0]["evidence"])
        self.assertEqual(items[1]["type"], "company")

    def test_pdf_text_keeps_header_affiliations_and_ignores_emails_and_body(self):
        items = affiliation_scan.affiliations_from_text(PDF_TEXT)
        names = [(item["institution"], item["country"]) for item in items]
        self.assertIn(("University of Lagos", "Nigeria"), names)
        self.assertIn(("Independent Researcher", "Canada"), names)
        self.assertFalse(any("Nowhere" in name for name, _ in names))
        self.assertFalse(any("@" in item["evidence"] for item in items))

    def test_scan_fills_missing_affiliations_and_never_overwrites(self):
        with mock.patch.object(affiliation_scan, "fetch_openalex", return_value={"2601.00001": WORK}), \
                mock.patch.object(affiliation_scan.time, "sleep"):
            state = affiliation_scan.run_scan(self.engine, use_openalex=True)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["checked"], 2)
        self.assertEqual(state["found"], 1)
        institution, country, analysis = self.row("2601.00001")
        self.assertEqual((institution, country), ("Virginia State University", "United States"))
        self.assertEqual(analysis["affiliation_method"], "openalex")
        self.assertFalse(analysis["affiliation_reviewed"])
        self.assertTrue(analysis["has_school_affiliation"])
        self.assertEqual(analysis["affiliation_source_url"], "https://openalex.org/W1")
        self.assertEqual(self.row("2601.00002")[0], None)
        self.assertEqual(self.row("2601.00003")[0], "Admin Entered Lab")

    def test_default_sources_read_pdfs_without_calling_openalex(self):
        with mock.patch.dict("os.environ", {"AFFILIATION_SOURCES": ""}), \
                mock.patch.object(affiliation_scan, "fetch_openalex", side_effect=AssertionError("OpenAlex used")), \
                mock.patch.object(affiliation_scan, "pdf_first_page_text", return_value=PDF_TEXT), \
                mock.patch.object(affiliation_scan.time, "sleep"):
            state = affiliation_scan.run_scan(self.engine)
        self.assertEqual(state["found_pdf"], 2)
        self.assertEqual(state["found_openalex"], 0)
        self.assertEqual(self.row("2601.00001")[0], "University of Lagos")

    def test_pdf_fallback_used_when_openalex_has_no_record(self):
        with mock.patch.object(affiliation_scan, "fetch_openalex", return_value={}), \
                mock.patch.object(affiliation_scan, "pdf_first_page_text", return_value=PDF_TEXT), \
                mock.patch.object(affiliation_scan.time, "sleep"):
            state = affiliation_scan.run_scan(self.engine, pdf_fallback=True, use_openalex=True)
        self.assertEqual(state["found_pdf"], 2)
        institution, country, analysis = self.row("2601.00002")
        self.assertEqual((institution, country), ("University of Lagos", "Nigeria"))
        self.assertEqual(analysis["affiliation_method"], "pdf_text")

    def test_budget_exhaustion_pauses_and_resumes_from_cursor(self):
        error = affiliation_scan.OpenAlexBudgetExhausted("budget used up")
        with mock.patch.object(affiliation_scan, "fetch_openalex", side_effect=error), \
                mock.patch.object(affiliation_scan.time, "sleep"):
            state = affiliation_scan.run_scan(self.engine, use_openalex=True)
        self.assertEqual(state["status"], "paused")
        self.assertIn("budget", state["error"])
        with mock.patch.object(affiliation_scan, "fetch_openalex", return_value={"2601.00001": WORK}), \
                mock.patch.object(affiliation_scan.time, "sleep"):
            state = affiliation_scan.run_scan(self.engine, use_openalex=True)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["found"], 1)


class OldStyleIdTests(unittest.TestCase):
    def test_pre_2007_ids_get_their_archive_back(self):
        from backend.arxiv_utils import abs_url, arxiv_path, pdf_url

        self.assertEqual(arxiv_path("0002216", ["cs.LG"]), "cs/0002216")
        self.assertEqual(arxiv_path("0002216", ["math.CO", "cs.DM"]), "math/0002216")
        self.assertEqual(arxiv_path("0002216", None, "cs.AI"), "cs/0002216")
        self.assertEqual(arxiv_path("2601.00001", ["cs.LG"]), "2601.00001")
        self.assertEqual(pdf_url("0002216", ["quant-ph", "cs.ET"]), "https://arxiv.org/pdf/quant-ph/0002216")
        self.assertEqual(abs_url("2601.00001"), "https://arxiv.org/abs/2601.00001")

    def test_scan_goes_newest_first_and_uses_fixed_pdf_paths(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_database_engine(f"sqlite:///{Path(tmp.name) / 'o.sqlite3'}")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, analysis TEXT NOT NULL, title TEXT, institution TEXT, "
                "country TEXT, primary_category TEXT, raw_metadata TEXT, published_at TEXT)"
            ))
            for arxiv_id, categories, published in [
                ("0002216", ["math.CO", "cs.DM"], "2000-02-20"),
                ("9912001", ["cs.AI"], "1999-12-01"),  # 1990s IDs sort above new ones as text
                ("2601.00001", ["cs.LG"], "2026-01-05"),
                ("2610.00001", ["cs.CV"], "2026-10-01"),
            ]:
                connection.execute(
                    text("INSERT INTO papers VALUES (:id, '{}', 't', NULL, NULL, :p, :r, :d)"),
                    {"id": arxiv_id, "p": categories[-1], "r": json.dumps({"categories": categories}), "d": published},
                )
        # a saved state from the old oldest-first scan
        affiliation_scan._ensure_tables(engine)
        with engine.begin() as connection:  # exactly what the old version saved: no "order" key
            connection.execute(text("INSERT INTO affiliation_scan_state (id, state_json) VALUES (1, :s)"),
                               {"s": json.dumps({"status": "paused", "cursor": "0002216", "checked": 50})})
        seen = []
        with mock.patch.dict("os.environ", {"AFFILIATION_SOURCES": ""}), \
                mock.patch.object(affiliation_scan, "pdf_first_page_text", side_effect=lambda path: seen.append(path) or PDF_TEXT), \
                mock.patch.object(affiliation_scan.time, "sleep"):
            state = affiliation_scan.run_scan(engine)
        self.assertEqual(seen, ["2610.00001", "2601.00001", "math/0002216", "cs/9912001"])
        self.assertEqual(state["checked"], 4)  # restarted cleanly, old cursor dropped
        self.assertEqual(state["status"], "complete")


if __name__ == "__main__":
    unittest.main()
