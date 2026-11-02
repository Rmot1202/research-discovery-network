import json
import tempfile
import unittest
from pathlib import Path

from backend import discovery_score as ds
from backend.database import create_database_engine, database_connection


def rows(spec):
    """spec: list of (count, field, institution, country, cluster)."""
    out, n = [], 0
    for count, field, institution, country, cluster in spec:
        for _ in range(count):
            out.append((f"id{n}", field, institution, country, cluster))
            n += 1
    return out


class NormalizationTests(unittest.TestCase):
    def test_least_common_is_100_most_common_is_0_on_log_scale(self):
        scores = ds.normalized_scores({"big": 100, "mid": 10, "small": 1})
        self.assertEqual(scores["big"], 0.0)
        self.assertEqual(scores["small"], 100.0)
        self.assertEqual(scores["mid"], 50.0)

    def test_equal_groups_are_not_underrepresented(self):
        self.assertEqual(ds.normalized_scores({"a": 5, "b": 5}), {"a": 0.0, "b": 0.0})


class WeightTests(unittest.TestCase):
    def test_default_weights_are_40_30_30(self):
        self.assertEqual(ds.parse_weights(""), {"topic": 0.4, "institution": 0.3, "geography": 0.3})

    def test_weights_accept_percentages_and_rescale(self):
        weights = ds.parse_weights("topic=50,institution=25,geo=25")
        self.assertAlmostEqual(weights["topic"], 0.5)
        self.assertAlmostEqual(weights["geography"], 0.25)

    def test_bad_weights_are_rejected(self):
        with self.assertRaises(ValueError):
            ds.parse_weights("citations=1")
        with self.assertRaises(ValueError):
            ds.parse_weights("topic=0,institution=0,geography=0")


class CombineTests(unittest.TestCase):
    def test_score_is_weighted_average_of_available_factors(self):
        result = ds.combine({"topic": 100, "institution": 50, "geography": 0})
        self.assertEqual(result["discovery_score"], 55.0)  # 40 + 15 + 0
        self.assertEqual(result["coverage"], 1.0)

    def test_missing_factors_are_left_out_and_reported(self):
        result = ds.combine({"topic": 80, "institution": None, "geography": None})
        self.assertEqual(result["discovery_score"], 80.0)
        self.assertEqual(result["coverage"], 0.4)

    def test_one_strong_factor_is_enough_to_flag(self):
        # Common topic and big institution, but a rarely seen country.
        result = ds.combine({"topic": 5, "institution": 10, "geography": 95})
        self.assertLess(result["discovery_score"], ds.OVERALL_THRESHOLD)
        self.assertTrue(result["underrepresented"])
        self.assertEqual(result["flags"], ["geography"])
        self.assertEqual(result["measures"], "representation")

    def test_nothing_known_gives_no_score(self):
        result = ds.combine({"topic": None, "institution": None, "geography": None})
        self.assertIsNone(result["discovery_score"])
        self.assertFalse(result["underrepresented"])


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.min_sample = ds.MIN_FIELD_SAMPLE
        ds.MIN_FIELD_SAMPLE = 5

    def tearDown(self):
        ds.MIN_FIELD_SAMPLE = self.min_sample

    def test_factors_are_measured_inside_the_field(self):
        index = ds.DiscoveryIndex(rows([
            # cs.CV: Big U dominates, Small U appears once
            (40, "cs.CV", "Big U", "United States", 0),
            (1, "cs.CV", "Small U", "Ghana", 1),
            # cs.CR: Small U dominates this field instead
            (40, "cs.CR", "Small U", "Ghana", None),
            (1, "cs.CR", "Big U", "United States", None),
        ]))
        cv_small = index.factors("cs.CV", "Small U", "Ghana", 1)
        cr_small = index.factors("cs.CR", "Small U", "Ghana", None)
        self.assertEqual(cv_small["institution"], 100.0)
        self.assertEqual(cr_small["institution"], 0.0)
        self.assertEqual(cv_small["geography"], 100.0)
        self.assertEqual(cv_small["topic"], 100.0)
        self.assertEqual(cv_small["topic_basis"], "subtopic")
        self.assertEqual(cr_small["topic_basis"], "category")  # cs.CR has not been clustered

    def test_unknown_values_and_small_fields_give_none(self):
        index = ds.DiscoveryIndex(rows([(3, "cs.RO", "A U", "Peru", None), (1, "cs.RO", "B U", "Chile", None)]))
        factors = index.factors("cs.RO", None, "Peru", None)
        self.assertIsNone(factors["institution"])
        self.assertIsNone(factors["geography"])  # only 4 documented papers, under the minimum
        result = ds.combine(factors)
        self.assertEqual(result["missing"]["institution"], "not_documented")
        self.assertEqual(result["missing"]["geography"], "small_field")

    def test_ranking_puts_highest_score_first(self):
        index = ds.DiscoveryIndex(rows([(30, "cs.CV", "Big U", "United States", 0), (1, "cs.CV", "Small U", "Ghana", 1)]))
        ranked = ds.ranked_ids(index)
        self.assertEqual(ranked[0][0], "id30")
        self.assertGreater(ranked[0][1]["discovery_score"], ranked[-1][1]["discovery_score"])


    def test_missing_factors_do_not_win_the_ranking(self):
        topic_only = ds.combine({"topic": 100, "institution": None, "geography": None})
        all_three = ds.combine({"topic": 80, "institution": 80, "geography": 80})
        self.assertGreater(ds.ranking_value(all_three), ds.ranking_value(topic_only))  # 80 vs 70


class DatabaseTests(unittest.TestCase):
    def test_load_index_works_before_and_after_clustering(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_database_engine(f"sqlite:///{Path(tmp) / 'd.sqlite3'}")
            with database_connection(engine) as connection:
                connection.execute(
                    "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, raw_metadata TEXT, analysis TEXT, "
                    "primary_category TEXT, title TEXT, abstract TEXT, authors TEXT, institution TEXT, "
                    "country TEXT, published_at TEXT, approval_status TEXT)"
                )
                for n in range(6):
                    connection.execute(
                        "INSERT INTO papers VALUES (?, '{}', '{}', 'cs.CV', 't', 'a', '[]', ?, NULL, '2026', 'pending')",
                        (f"p{n}", "Big U" if n else "Small U"),
                    )
            ds.invalidate()
            index = ds.load_index(engine, fresh=True)
            self.assertEqual(index.field_sizes, {"cs.CV": 6})
            with database_connection(engine) as connection:
                connection.execute("CREATE TABLE paper_subtopics (arxiv_id TEXT PRIMARY KEY, category TEXT, cluster_id INTEGER, distance REAL)")
                connection.execute("CREATE TABLE subtopics (category TEXT, cluster_id INTEGER, label TEXT)")
                connection.execute("INSERT INTO paper_subtopics VALUES ('p0', 'cs.CV', 3, 0.1)")
                connection.execute("INSERT INTO subtopics VALUES ('cs.CV', 3, 'tiny topic')")
            index = ds.load_index(engine, fresh=True)
            self.assertEqual(index.papers["p0"][3], 3)
            self.assertEqual(index.subtopic_labels[("cs.CV", "3")], "tiny topic")
            report = ds.field_report(index, "cs.CV")
            self.assertEqual(report["least_represented"]["subtopics"][0]["name"], "tiny topic")
            self.assertIn("not a visibility measure", report["measures"])
            json.dumps(report)


if __name__ == "__main__":
    unittest.main()
