import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from backend import main, openalex_import
from backend.arxiv_utils import abs_url, is_openalex_id
from backend.database import create_database_engine


def sample_work(**overrides):
    work = {
        "id": "https://openalex.org/W4400000001",
        "doi": "https://doi.org/10.1145/1234567",
        "display_name": "Secure Federated Intrusion Detection for Industrial Control Networks",
        "publication_date": "2026-11-05",
        "type": "article",
        "abstract_inverted_index": {
            "We": [0], "propose": [1], "an": [2], "intrusion": [3], "detection": [4], "method": [5],
            **{f"word{i}": [6 + i] for i in range(40)},
        },
        "authorships": [{
            "author": {"display_name": "Ada Example"},
            "raw_affiliation_strings": ["Virginia State University, Petersburg, VA, USA"],
            "institutions": [{"display_name": "Virginia State University", "country_code": "US", "type": "education"}],
        }],
        "primary_topic": {"display_name": "Network Security and Intrusion Detection",
                          "subfield": {"id": "https://openalex.org/subfields/1705",
                                       "display_name": "Computer Networks and Communications"}},
        "topics": [],
        "primary_location": {"landing_page_url": "https://dl.acm.org/doi/10.1145/1234567",
                             "source": {"display_name": "ACM CCS"}},
        "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
        "indexed_in": ["crossref"],
        "cited_by_count": 3,
    }
    work.update(overrides)
    return work


class OpenAlexMappingTests(unittest.TestCase):
    def test_abstract_is_rebuilt_from_the_inverted_index(self):
        text = openalex_import.abstract_from_inverted_index({"world": [1], "Hello": [0]})
        self.assertEqual(text, "Hello world")

    def test_topic_keywords_refine_the_subfield_category(self):
        paper = openalex_import.paper_from_work(sample_work())
        self.assertEqual(paper["primary_category"], "cs.CR")
        self.assertEqual(paper["arxiv_id"], "W4400000001")
        self.assertEqual(paper["source"], "openalex")
        self.assertEqual(paper["url"], "https://doi.org/10.1145/1234567")

    def test_works_already_on_arxiv_are_skipped(self):
        self.assertIsNone(openalex_import.paper_from_work(sample_work(indexed_in=["arxiv", "crossref"])))
        self.assertIsNone(openalex_import.paper_from_work(sample_work(doi="https://doi.org/10.48550/arxiv.2601.00001")))

    def test_openalex_ids_link_to_openalex(self):
        self.assertTrue(is_openalex_id("W4400000001"))
        self.assertFalse(is_openalex_id("2601.00001"))
        self.assertEqual(abs_url("W4400000001"), "https://openalex.org/works/W4400000001")

    def test_filter_uses_free_publication_date_filters(self):
        filters = openalex_import.base_filter("2026-11-01", "2026-11-02")
        self.assertIn("from_publication_date:2026-11-01", filters)
        self.assertNotIn("created_date", filters)


class LoadedTodayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.original = main.ENGINE
        main.ENGINE = create_database_engine(f"sqlite:///{Path(cls.tmp.name) / 'papers.sqlite3'}")
        main.init_db()

    @classmethod
    def tearDownClass(cls):
        main.ENGINE.dispose()
        main.ENGINE = cls.original
        cls.tmp.cleanup()

    def test_openalex_page_is_saved_with_affiliation_and_shows_in_todays_queue(self):
        new, affiliated = openalex_import.save_page([sample_work()])
        self.assertEqual((new, affiliated), (1, 1))
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "t"}):
            queue = main.admin_review_queue(authorization="Bearer t", weights=None, day=None,
                                            tz_offset_minutes=0, source=None, status=None, limit=30, offset=0)
            today = main.admin_loaded_today(authorization="Bearer t", day=None, tz_offset_minutes=0)
            yesterday = main.admin_review_queue(authorization="Bearer t", weights=None, day="2000-01-01",
                                                tz_offset_minutes=0, source=None, status=None, limit=30, offset=0)
        self.assertEqual(queue["total"], 1)
        paper = queue["papers"][0]
        self.assertEqual(paper["source"], "openalex")
        self.assertEqual(paper["institution"], "Virginia State University")
        self.assertEqual(paper["pdf_url"], "https://example.org/paper.pdf")
        self.assertEqual(today["by_source"]["openalex"], 1)
        self.assertEqual(yesterday["total"], 0)

    def test_day_window_follows_the_admin_time_zone(self):
        day, start, end = main.loaded_day_window("2026-11-11", 300)  # New York, winter
        self.assertEqual(start, "2026-11-11T05:00:00+00:00")
        self.assertEqual(end, "2026-11-12T05:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
