import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import paper_chat
from backend.database import create_database_engine, database_connection


def make_archive(path: Path):
    engine = create_database_engine(f"sqlite:///{path}")
    with database_connection(engine) as connection:
        connection.execute(
            "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, raw_metadata TEXT NOT NULL, analysis TEXT NOT NULL, "
            "primary_category TEXT NOT NULL, title TEXT NOT NULL, abstract TEXT NOT NULL, authors TEXT NOT NULL, "
            "institution TEXT, country TEXT, published_at TEXT NOT NULL, approval_status TEXT NOT NULL DEFAULT 'pending')"
        )
        connection.execute(
            "CREATE TABLE paper_categories (arxiv_id TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY (arxiv_id, category))"
        )
        papers = [
            ("2401.00001", "cs.RO", "Pose estimation for robot grasping", "We estimate object pose for robotic grasping with depth cameras."),
            ("2401.00002", "cs.CR", "Side-channel attacks on 3D printers", "Acoustic side channels leak G-code from additive manufacturing machines."),
            ("2401.00003", "cs.CL", "Instruction tuning for language models", "We study instruction tuning of large language models."),
        ]
        for arxiv_id, category, title, abstract in papers:
            raw = {"arxiv_id": arxiv_id, "url": f"https://arxiv.org/abs/{arxiv_id}", "title": title, "abstract": abstract,
                   "authors": ["Example Author"], "categories": [category]}
            connection.execute(
                "INSERT INTO papers (arxiv_id, raw_metadata, analysis, primary_category, title, abstract, authors, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (arxiv_id, json.dumps(raw), "{}", category, title, abstract, json.dumps(["Example Author"]), "2024-01-02T00:00:00Z"),
            )
            connection.execute("INSERT INTO paper_categories (arxiv_id, category) VALUES (?, ?)", (arxiv_id, category))
    return engine


class PaperChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_archive(Path(self.tmp.name) / "papers.sqlite3")
        paper_chat._last_index_sync.clear()

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_keywords_drop_question_filler(self):
        self.assertEqual(paper_chat.extract_keywords("What papers are about 3D printer security?"), ["3d", "printer", "security"])

    def test_full_text_retrieval_ranks_matching_paper_first(self):
        with patch.dict(os.environ, {"AI_EMBEDDING_MODEL": ""}):
            papers, method = paper_chat.retrieve_papers(self.engine, "Which papers attack 3D printers?")
        self.assertEqual(method, "full-text")
        self.assertEqual(papers[0]["arxiv_id"], "2401.00002")
        self.assertEqual(papers[0]["url"], "https://arxiv.org/abs/2401.00002")

    def test_new_papers_are_indexed_on_next_sync(self):
        paper_chat.ensure_search_index(self.engine, force=True)
        with database_connection(self.engine) as connection:
            connection.execute(
                "INSERT INTO papers (arxiv_id, raw_metadata, analysis, primary_category, title, abstract, authors, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2402.00009", "{}", "{}", "cs.CV", "Thermal camera tracking", "Tracking with thermal cameras.", "[]", "2024-02-01"),
            )
        paper_chat.ensure_search_index(self.engine, force=True)
        papers, _ = paper_chat.retrieve_papers(self.engine, "thermal tracking")
        self.assertEqual(papers[0]["arxiv_id"], "2402.00009")

    def test_category_scope_limits_results(self):
        papers, _ = paper_chat.retrieve_papers(self.engine, "pose language printers", category="cs.CL")
        self.assertEqual([paper["arxiv_id"] for paper in papers], ["2401.00003"])

    def test_answer_sends_retrieved_papers_to_model(self):
        papers, _ = paper_chat.retrieve_papers(self.engine, "robot grasping pose")
        captured = {}

        def fake_request(resource, payload):
            captured["payload"] = payload
            return {"choices": [{"message": {"content": "Grasping is covered in [arXiv:2401.00001]."}}]}

        with patch.dict(os.environ, {"AI_CHAT_MODEL": "test-model"}), patch.object(paper_chat, "ai_request", fake_request):
            answer = paper_chat.answer_question("robot grasping?", [], papers, {"total_papers": 3}, None)
        self.assertIn("2401.00001", answer)
        self.assertIn("Pose estimation for robot grasping", captured["payload"]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
