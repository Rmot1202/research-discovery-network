"""Bookkeeping shared by every importer (arXiv daily, arXiv backfill, OpenAlex).

- `ensure_paper_columns` adds `source` and `ingested_at` to the papers table, so the admin
  dashboard can show exactly what was loaded on a given day.
- `log_run` records one row per importer run (what was added, why it stopped, credits used).
- `storage_ok` keeps the hosted database under its free-plan size (Neon Free = 1 GB per project),
  so a big import day cannot lock the database by filling it.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Leave headroom under Neon Free's 1 GB: indexes, embeddings and summaries grow after import.
DEFAULT_STORAGE_LIMIT_MB = 900


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_paper_columns(engine: Engine) -> None:
    """Add papers.source and papers.ingested_at if this database predates them."""
    existing = {column["name"] for column in inspect(engine).get_columns("papers")}
    with engine.begin() as connection:
        if "source" not in existing:
            connection.execute(text("ALTER TABLE papers ADD COLUMN source TEXT"))
        if "ingested_at" not in existing:
            connection.execute(text("ALTER TABLE papers ADD COLUMN ingested_at TEXT"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_papers_ingested ON papers(ingested_at)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source)"))
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS import_runs ("
            "id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, "
            "source TEXT NOT NULL, inserted INTEGER NOT NULL, details TEXT NOT NULL)"
        ) if engine.dialect.name == "sqlite" else text(
            "CREATE TABLE IF NOT EXISTS import_runs ("
            "id SERIAL PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, "
            "source TEXT NOT NULL, inserted INTEGER NOT NULL, details TEXT NOT NULL)"
        ))


def log_run(engine: Engine, source: str, started_at: str, inserted: int, details: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO import_runs (started_at, finished_at, source, inserted, details) "
                 "VALUES (:s, :f, :src, :n, :d)"),
            {"s": started_at, "f": now_iso(), "src": source, "n": inserted,
             "d": json.dumps(details, ensure_ascii=False, default=str)},
        )


def recent_runs(engine: Engine, since_iso: str, limit: int = 20) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT started_at, finished_at, source, inserted, details FROM import_runs "
                 "WHERE finished_at >= :since ORDER BY finished_at DESC LIMIT :n"),
            {"since": since_iso, "n": limit},
        ).all()
    return [
        {"started_at": row[0], "finished_at": row[1], "source": row[2], "inserted": row[3],
         "details": json.loads(row[4] or "{}")}
        for row in rows
    ]


def database_size_mb(engine: Engine) -> float | None:
    """Size of the hosted database in MB (PostgreSQL only; SQLite has no quota to protect)."""
    if engine.dialect.name != "postgresql":
        return None
    with engine.connect() as connection:
        size = connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    return round(size / 1_048_576, 1)


def storage_limit_mb() -> float:
    return float(os.environ.get("DB_STORAGE_LIMIT_MB", DEFAULT_STORAGE_LIMIT_MB))


def storage_ok(engine: Engine) -> bool:
    size = database_size_mb(engine)
    return size is None or size < storage_limit_mb()
