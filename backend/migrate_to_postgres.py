from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from backend.main import connect_db


SOURCE = Path(os.environ.get("PAPERS_DB", Path(__file__).parent / "data" / "papers.sqlite3"))
BATCH_SIZE = 500


def migrate(source_path: Path = SOURCE) -> dict[str, int]:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source database was not found: {source_path}")
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        paper_count = source.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        with connect_db() as target:
            target_count = target.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        if paper_count == 0:
            raise RuntimeError("The SQLite database contains no papers to migrate.")

        for offset in range(0, paper_count, BATCH_SIZE):
            papers = source.execute(
                """
                SELECT arxiv_id, raw_metadata, analysis, primary_category, title, abstract,
                    authors, institution, country, published_at, approval_status
                FROM papers ORDER BY arxiv_id LIMIT ? OFFSET ?
                """,
                (BATCH_SIZE, offset),
            ).fetchall()
            with connect_db() as target:
                target.executemany(
                    """
                    INSERT INTO papers (
                        arxiv_id, raw_metadata, analysis, primary_category, title, abstract,
                        authors, institution, country, published_at, approval_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    [tuple(row) for row in papers],
                )

        category_count = 0
        categories_exist = source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'paper_categories'"
        ).fetchone()
        if categories_exist:
            assignment_count = source.execute("SELECT COUNT(*) FROM paper_categories").fetchone()[0]
            for offset in range(0, assignment_count, BATCH_SIZE * 10):
                memberships = source.execute(
                    "SELECT arxiv_id, category FROM paper_categories ORDER BY arxiv_id, category LIMIT ? OFFSET ?",
                    (BATCH_SIZE * 10, offset),
                ).fetchall()
                if not memberships:
                    break
                category_count += len(memberships)
                with connect_db() as target:
                    target.executemany(
                        "INSERT INTO paper_categories (arxiv_id, category) VALUES (?, ?) "
                        "ON CONFLICT DO NOTHING",
                        [tuple(row) for row in memberships],
                    )

        ingest_table = source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ingest_state'"
        ).fetchone()
        ingest_row = (
            source.execute("SELECT state_json FROM ingest_state WHERE id = 1").fetchone()
            if ingest_table
            else None
        )
        if ingest_row:
            with connect_db() as target:
                target.execute(
                    "INSERT INTO ingest_state (id, state_json) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
                    (ingest_row["state_json"],),
                )
        with connect_db() as target:
            migrated_count = target.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        return {
            "source_papers": paper_count,
            "existing_target_papers": target_count,
            "target_papers_after_migration": migrated_count,
            "source_category_assignments": category_count,
        }
    finally:
        source.close()


if __name__ == "__main__":
    print(json.dumps(migrate(), indent=2))
