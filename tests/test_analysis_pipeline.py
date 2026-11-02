import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import analysis_pipeline as pipeline
from backend import subtopics
from backend.ai_service import coerce_research_analysis
from backend.database import create_database_engine, database_connection

THEMES = {
    "vision": "image segmentation convolutional pixels camera detection",
    "language": "language translation tokens transformer text sentences",
    "robots": "robot grasping manipulation arm locomotion control",
}


def make_papers(per_theme=14):
    papers = []
    for theme_index, (theme, words) in enumerate(THEMES.items()):
        for index in range(per_theme):
            arxiv_id = f"2610.{theme_index}{index:04d}"
            papers.append((
                arxiv_id, theme,
                f"{theme.title()} paper {index} on {words.split()[index % 5]} {words.split()[(index + 1) % 5]}",
                f"We study {words} and {words}. Variant {index}.",
            ))
    return papers


class AnalysisPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_database_engine(f"sqlite:///{Path(self.tmp.name) / 'p.sqlite3'}")
        with database_connection(self.engine) as connection:
            connection.execute(
                "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, raw_metadata TEXT NOT NULL, analysis TEXT NOT NULL, "
                "primary_category TEXT NOT NULL, title TEXT NOT NULL, abstract TEXT NOT NULL, authors TEXT NOT NULL, "
                "institution TEXT, country TEXT, published_at TEXT NOT NULL, approval_status TEXT NOT NULL DEFAULT 'pending')"
            )
            for arxiv_id, _theme, title, abstract in make_papers():
                connection.execute(
                    "INSERT INTO papers VALUES (?, '{}', '{}', 'cs.CV', ?, ?, '[\"A. Author\"]', NULL, NULL, ?, 'pending')",
                    (arxiv_id, title, abstract, f"2026-10-{1 + int(arxiv_id[-2:]) % 28:02d}T00:00:00Z"),
                )
        subtopics.invalidate()
        self.quiet = lambda _message: None

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def connect(self):
        return database_connection(self.engine)

    def test_embeddings_are_stored_once_and_resume(self):
        first = pipeline.embed_papers(self.engine, pipeline.HashingEmbedder(), limit=10, log=self.quiet)
        self.assertEqual(first["embedded"], 10)
        rest = pipeline.embed_papers(self.engine, pipeline.HashingEmbedder(), log=self.quiet)
        self.assertEqual(rest["embedded"], 32)
        again = pipeline.embed_papers(self.engine, pipeline.HashingEmbedder(), log=self.quiet)
        self.assertEqual(again["embedded"], 0)
        with self.connect() as connection:
            vector = connection.execute("SELECT vector FROM paper_embeddings LIMIT 1").fetchone()["vector"]
        self.assertEqual(len(pipeline.decode_vector(vector)), 512)

    def test_subtopics_separate_themes_and_get_readable_labels(self):
        pipeline.embed_papers(self.engine, pipeline.HashingEmbedder(), log=self.quiet)
        result = pipeline.cluster_papers(self.engine, log=self.quiet)
        self.assertEqual(result["regrouped"], 1)
        with self.connect() as connection:
            groups = connection.execute("SELECT cluster_id, label FROM subtopics").fetchall()
            members = connection.execute("SELECT arxiv_id, cluster_id FROM paper_subtopics").fetchall()
        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(len(members), 42)
        self.assertTrue(all(group["label"] and group["label"] != "General" for group in groups))
        # Papers from one theme should mostly share a group.
        by_theme = {}
        for member in members:
            by_theme.setdefault(member["arxiv_id"][5], []).append(member["cluster_id"])
        for clusters in by_theme.values():
            most_common = max(clusters.count(cluster) for cluster in set(clusters))
            self.assertGreaterEqual(most_common / len(clusters), 0.7)

    def test_new_papers_join_existing_groups_without_regrouping(self):
        pipeline.embed_papers(self.engine, pipeline.HashingEmbedder(), log=self.quiet)
        pipeline.cluster_papers(self.engine, log=self.quiet)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO papers VALUES ('2610.99999', '{}', '{}', 'cs.CV', 'Robot arm grasping', "
                "'robot grasping manipulation arm', '[]', NULL, NULL, '2026-10-30T00:00:00Z', 'pending')"
            )
        pipeline.embed_papers(self.engine, pipeline.HashingEmbedder(), log=self.quiet)
        result = pipeline.cluster_papers(self.engine, log=self.quiet)
        self.assertEqual((result["regrouped"], result["assigned"]), (0, 1))

    def test_summaries_are_written_resumable_and_failures_recorded(self):
        calls = []

        def fake(paper):
            calls.append(paper["title"])
            if "paper 3 " in paper["title"]:
                raise RuntimeError("model timeout")
            return {field: f"{field} text" for field in pipeline_fields()}

        result = pipeline.summarize_papers(self.engine, limit=12, summarizer=fake, model_name="tiny", log=self.quiet)
        self.assertEqual(result["summarized"] + result["failed"], 12)
        with self.connect() as connection:
            done = connection.execute("SELECT COUNT(*) FROM paper_summary_runs WHERE status = 'done'").fetchone()[0]
            row = connection.execute(
                "SELECT p.analysis FROM papers p JOIN paper_summary_runs r ON r.arxiv_id = p.arxiv_id "
                "WHERE r.status = 'done' LIMIT 1"
            ).fetchone()
        self.assertEqual(done, result["summarized"])
        analysis = json.loads(row["analysis"])
        self.assertEqual(analysis["summary"], "summary text")
        self.assertEqual(analysis["summary_model"], "tiny")
        calls.clear()
        pipeline.summarize_papers(self.engine, limit=5, summarizer=fake, model_name="tiny", log=self.quiet)
        self.assertEqual(len(calls), 5)
        status = pipeline.status(self.engine)
        self.assertEqual(status["summarized"], result["summarized"] + 5)

    def test_admin_written_summaries_are_kept(self):
        with self.connect() as connection:
            connection.execute(
                "UPDATE papers SET analysis = ?, approval_status = 'approved' WHERE arxiv_id = '2610.00000'",
                (json.dumps({"summary": "Editor summary"}),),
            )
        pipeline.summarize_papers(self.engine, limit=1, summarizer=lambda _p: self.fail("model called"),
                                  model_name="tiny", log=self.quiet)
        with self.connect() as connection:
            analysis = json.loads(connection.execute(
                "SELECT analysis FROM papers WHERE arxiv_id = '2610.00000'").fetchone()["analysis"])
        self.assertEqual(analysis["summary"], "Editor summary")

    def test_local_model_is_the_default_provider(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            model = pipeline.configure_llm()
            import os
            self.assertEqual(os.environ["AI_BASE_URL"], pipeline.DEFAULT_LOCAL_LLM_URL)
        self.assertEqual(model, pipeline.DEFAULT_LOCAL_LLM)

    def test_drilldown_uses_stored_subtopics_with_paper_cards(self):
        pipeline.embed_papers(self.engine, pipeline.HashingEmbedder(), log=self.quiet)
        pipeline.cluster_papers(self.engine, log=self.quiet)
        body = subtopics.topic_drilldown(self.connect, "cs.CV", 4, "public")
        self.assertEqual(body["source"], "embeddings")
        self.assertEqual(body["total_papers"], 42)
        self.assertTrue(all(1 <= len(cluster["papers"]) <= 4 for cluster in body["clusters"]))
        card = body["clusters"][0]["papers"][0]
        self.assertTrue(card["url"].startswith("https://arxiv.org/abs/"))

    def test_drilldown_falls_back_to_keywords_before_the_pipeline_runs(self):
        pipeline.ensure_tables(self.engine)
        body = subtopics.topic_drilldown(self.connect, "cs.CV", 3, "public")
        self.assertEqual(body["source"], "keywords")
        self.assertGreaterEqual(len(body["clusters"]), 2)
        self.assertEqual(sum(cluster["size"] for cluster in body["clusters"]), 42)


def pipeline_fields():
    return ("summary", "key_contribution", "application", "relevance", "topics", "methods", "context")


class LenientParsingTests(unittest.TestCase):
    def test_small_model_answers_are_cleaned_up(self):
        content = '```json\n{"Summary": "Plain words.", "topics": ["a", "b"], "methods": "x"}\n```'
        parsed = coerce_research_analysis(content)
        self.assertEqual(parsed["summary"], "Plain words.")
        self.assertEqual(parsed["topics"], "a, b")
        self.assertEqual(parsed["context"], "Not stated in the abstract")

    def test_answer_without_summary_is_rejected(self):
        with self.assertRaises(ValueError):
            coerce_research_analysis('{"topics": "a"}')


if __name__ == "__main__":
    unittest.main()
