# Tests always use a throwaway SQLite file, never the DATABASE_URL in your shell (it could be Neon).
import os as _os
import tempfile as _tempfile

_os.environ["RDN_SKIP_ENV_FILES"] = "1"  # never read .env.local (Neon) during tests
_os.environ.pop("DATABASE_URL", None)
_os.environ.pop("DATABASE_URL_UNPOOLED", None)
_os.environ["PAPERS_DB"] = _os.path.join(_tempfile.mkdtemp(), "papers.sqlite3")

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import main
from backend.database import create_database_engine


class LegacyIngestTests(unittest.TestCase):
    """The old full import is retired once the daily job has run, so its stale error disappears."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_engine = main.ENGINE
        main.ENGINE = create_database_engine(f"sqlite:///{Path(self.tmp.name) / 'papers.sqlite3'}")
        main.init_db()
        main.write_ingest_state({**main.default_ingest_state(), "status": "error", "pages_processed": 53,
                                 "ingested_papers": 68616, "resumption_token": "old-cursor",
                                 "error": "The read operation timed out"})

    def tearDown(self) -> None:
        main.ENGINE.dispose()
        main.ENGINE = self.previous_engine
        self.tmp.cleanup()

    def _daily_ran(self) -> None:
        with main.connect_db() as connection:
            connection.execute(
                "INSERT INTO paper_update_state (id, state_json) VALUES (1, ?)",
                (json.dumps({"last_run_at": "2026-11-11T21:34:00+00:00", "backfill_until": "2026-10-01",
                             "last_result": {"new_papers": 1200, "backfill_new_papers": 16000}}),),
            )

    def test_error_stays_until_the_daily_job_has_run(self) -> None:
        progress = main.ingest_progress()
        self.assertEqual(progress["status"], "error")
        self.assertIsNone(progress["daily"])
        self.assertNotIn("resumption_token", progress)

    def test_retired_after_daily_run(self) -> None:
        self._daily_ran()
        progress = main.ingest_progress()
        self.assertEqual(progress["status"], "retired")
        self.assertIsNone(progress["error"])
        self.assertEqual(progress["daily"]["new_papers"], 17200)
        self.assertEqual(progress["pages_processed"], 53)
        self.assertIsNone(main.read_ingest_state()["resumption_token"])

    def test_oai_page_retries_timeouts(self) -> None:
        calls = {"n": 0}

        class Body:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords/></OAI-PMH>'

        def flaky(request, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise socket.timeout("The read operation timed out")
            return Body()

        with patch.object(main.urllib.request, "urlopen", flaky), patch.object(main.time, "sleep"):
            papers, token, _ = main.fetch_oai_page(None)
        self.assertEqual(calls["n"], 3)
        self.assertEqual((papers, token), ([], None))

    def test_oai_page_gives_up_after_three_tries(self) -> None:
        def always_slow(request, timeout):
            raise socket.timeout("The read operation timed out")

        with patch.object(main.urllib.request, "urlopen", always_slow), patch.object(main.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 tries"):
                main.fetch_oai_page(None)


if __name__ == "__main__":
    unittest.main()
