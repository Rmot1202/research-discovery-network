# Never read .env.local / .env (they may hold the Neon connection strings) during tests.
import os as _os

_os.environ["RDN_SKIP_ENV_FILES"] = "1"
_os.environ.pop("DATABASE_URL", None)
_os.environ.pop("DATABASE_URL_UNPOOLED", None)

import json
import tempfile
import unittest
from pathlib import Path

from backend import paper_chat, research_gaps
from backend.database import create_database_engine, database_connection


def add_paper(connection, arxiv_id, categories, title, abstract, published, authors, institution=None, country=None):
    connection.execute(
        "INSERT INTO papers (arxiv_id, raw_metadata, analysis, primary_category, title, abstract, authors, "
        "institution, country, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (arxiv_id, "{}", "{}", categories[0], title, abstract, json.dumps(authors), institution, country, published),
    )
    for category in categories:
        connection.execute("INSERT INTO paper_categories (arxiv_id, category) VALUES (?, ?)", (arxiv_id, category))


class ResearchGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_database_engine(f"sqlite:///{Path(self.tmp.name) / 'papers.sqlite3'}")
        research_gaps._cache.update({"built": 0.0, "count": -1, "corpus": None})
        paper_chat._last_index_sync.clear()
        with database_connection(self.engine) as connection:
            connection.execute(
                "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, raw_metadata TEXT NOT NULL, analysis TEXT NOT NULL, "
                "primary_category TEXT NOT NULL, title TEXT NOT NULL, abstract TEXT NOT NULL, authors TEXT NOT NULL, "
                "institution TEXT, country TEXT, published_at TEXT NOT NULL, approval_status TEXT NOT NULL DEFAULT 'pending')"
            )
            connection.execute("CREATE TABLE paper_categories (arxiv_id TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY (arxiv_id, category))")
            connection.execute("CREATE TABLE ingest_state (id INTEGER PRIMARY KEY, state_json TEXT NOT NULL)")
            # A large, growing, connected machine-learning field with surveys and many authors and countries.
            for index in range(120):
                year = 2025 if index < 80 else 2022
                add_paper(
                    connection, f"25{index:02d}.{index:05d}", ["cs.LG", "cs.AI"] if index % 2 else ["cs.LG"],
                    "A survey of deep learning" if index % 10 == 0 else f"Neural network method {index}",
                    "Deep learning neural networks.", f"{year}-06-01T00:00:00Z", [f"Author {index}"],
                    institution=f"University {index % 12}", country=f"Country {index % 12}",
                )
            # A tiny, flat, isolated field written by one group in one place.
            for index in range(6):
                add_paper(
                    connection, f"23{index:02d}.0000{index}", ["cs.SC"], f"Symbolic integration trick {index}",
                    "Symbolic computation of integrals.", "2023-03-01T00:00:00Z", ["Same Person"],
                    institution="Only Lab", country="One Country",
                )

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_small_isolated_topic_scores_more_under_researched_than_large_topic(self):
        report = research_gaps.topic_gap_report(self.engine)
        topics = {topic["code"]: topic for topic in report["topics"]}
        self.assertGreater(topics["cs.SC"]["index"], topics["cs.LG"]["index"])
        self.assertIn(topics["cs.SC"]["verdict"], {"Under-researched", "Under-researched, growing"})
        self.assertEqual(topics["cs.SC"]["components"]["isolation"], 100.0)
        self.assertEqual(topics["cs.SC"]["components"]["concentration"], 100.0)
        self.assertEqual(topics["cs.SC"]["confidence"], "low")
        with_papers = [topic["code"] for topic in report["topics"] if topic["metrics"]["papers"]]
        self.assertEqual(with_papers[-1], "cs.LG")  # most developed of the topics that have papers
        self.assertEqual(report["topics"][-1]["metrics"]["papers"], 0)  # empty topics are listed last
        self.assertIsNotNone(report["methodology"]["coverage_note"])

    def test_underrepresentation_requires_enough_documented_affiliations(self):
        topics = {topic["code"]: topic for topic in research_gaps.topic_gap_report(self.engine)["topics"]}
        self.assertIsNone(topics["cs.SC"]["representation"]["index"])
        self.assertEqual(topics["cs.SC"]["representation"]["verdict"], "Not enough documented affiliations")
        learning = topics["cs.LG"]["representation"]
        self.assertIsNotNone(learning["index"])
        self.assertLess(learning["index"], 40)
        self.assertEqual(learning["verdict"], "Broadly represented")

    def test_concentration_is_high_when_one_place_dominates(self):
        self.assertEqual(research_gaps._concentration(["A"] * 20), 100.0)
        self.assertGreater(research_gaps._concentration(["A"] * 18 + ["B", "C"]), 70)
        self.assertLess(research_gaps._concentration([f"Place {i}" for i in range(40)]), 10)

    def test_idea_report_matches_all_keywords(self):
        report = research_gaps.idea_gap_report(self.engine, "symbolic integration")
        self.assertEqual(report["metrics"]["papers"], 6)
        self.assertEqual(report["main_topics"][0]["code"], "cs.SC")
        self.assertEqual(len(report["sample_papers"]), 6)
        missing = research_gaps.idea_gap_report(self.engine, "quantum blockchain")
        self.assertEqual(missing["verdict"], "No papers found")


if __name__ == "__main__":
    unittest.main()
