"""Daily update: import papers arXiv added since the last run, then look up their affiliations.

Run by the scheduled GitHub Actions workflow (.github/workflows/update-papers.yml) against the
hosted database, or by hand:

    python -m backend.update_papers                  # since the last run (first run: last 3 days)
    python -m backend.update_papers --since 2026-10-01
    python -m backend.update_papers --no-affiliations

New papers are always added. Whether they appear in the homepage "Featured" list right away
depends on PUBLISH_MODE on the web service (auto = yes, review = after approval).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from backend import affiliation_scan, main as app_main
from backend.main import OAI_REQUEST_INTERVAL, connect_db, fetch_oai_page, insert_papers

FIRST_RUN_DAYS = 3
OVERLAP_DAYS = 1  # re-ask for the last day so nothing slips between runs (duplicates are ignored)


def _ensure_table() -> None:
    with connect_db() as connection:  # separate from ingest_state, which tracks the full backfill
        connection.execute(
            "CREATE TABLE IF NOT EXISTS paper_update_state (id INTEGER PRIMARY KEY, state_json TEXT NOT NULL)"
        )


def read_update_state() -> dict[str, Any]:
    _ensure_table()
    with connect_db() as connection:
        row = connection.execute("SELECT state_json FROM paper_update_state WHERE id = 1").fetchone()
    return json.loads(row["state_json"]) if row else {}


def write_update_state(state: dict[str, Any]) -> None:
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO paper_update_state (id, state_json) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
            (json.dumps(state),),
        )


def import_since(from_date: str, max_pages: int | None = None) -> dict[str, int]:
    token: str | None = None
    pages = records = inserted = 0
    while True:
        papers, token, _total = fetch_oai_page(token, from_date if token is None else None)
        pages += 1
        records += len(papers)
        inserted += insert_papers(papers)
        print(f"  page {pages}: {len(papers):,} records, {inserted:,} new papers so far", file=sys.stderr, flush=True)
        if not token or (max_pages and pages >= max_pages):
            break
        time.sleep(OAI_REQUEST_INTERVAL)  # arXiv asks harvesters to pace requests
    return {"pages": pages, "records": records, "new_papers": inserted}


def enrich_recent(days: int = 60, max_papers: int = 5000) -> dict[str, int]:
    """OpenAlex affiliations for recent papers that still have none.

    New arXiv papers usually reach OpenAlex within days, so recent papers are re-checked on
    each run until they're found or older than `days`.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    with app_main.ENGINE.connect() as connection:
        ids = [row[0] for row in connection.execute(
            text("SELECT arxiv_id FROM papers WHERE institution IS NULL AND published_at >= :s "
                 "ORDER BY published_at DESC LIMIT :n"),
            {"s": since, "n": max_papers},
        ).all()]
    checked = found = 0
    for start in range(0, len(ids), affiliation_scan.OPENALEX_BATCH):
        batch = ids[start:start + affiliation_scan.OPENALEX_BATCH]
        try:
            works = affiliation_scan.fetch_openalex(batch)
        except affiliation_scan.OpenAlexBudgetExhausted as error:
            print(f"  OpenAlex daily budget used up, stopping early: {error}", file=sys.stderr, flush=True)
            break
        for arxiv_id in batch:
            work = works.get(arxiv_id)
            if work and affiliation_scan.save_affiliations(
                app_main.ENGINE, arxiv_id, affiliation_scan.affiliations_from_openalex(work), "openalex", work["id"],
            ):
                found += 1
        checked += len(batch)
        time.sleep(0.2)
    affiliation_scan._invalidate_gap_cache()
    return {"recent_checked": checked, "affiliations_found": found}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="Import papers added on/after this date (YYYY-MM-DD).")
    parser.add_argument("--max-pages", type=int, default=None, help="Stop after this many arXiv pages.")
    parser.add_argument("--no-affiliations", action="store_true", help="Skip the OpenAlex affiliation lookup.")
    args = parser.parse_args()

    state = read_update_state()
    started = datetime.now(UTC)
    if args.since:
        from_date = args.since
    elif state.get("last_run_date"):
        from_date = (date.fromisoformat(state["last_run_date"]) - timedelta(days=OVERLAP_DAYS)).isoformat()
    else:
        from_date = (started.date() - timedelta(days=FIRST_RUN_DAYS)).isoformat()
    print(f"Importing arXiv CS papers added since {from_date}...", file=sys.stderr, flush=True)
    result: dict[str, Any] = {"from_date": from_date, **import_since(from_date, args.max_pages)}
    if not args.no_affiliations:
        print("Looking up affiliations for recent papers...", file=sys.stderr, flush=True)
        result.update(enrich_recent())
    with connect_db() as connection:
        result["total_papers"] = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    state.update({"last_run_date": started.date().isoformat(), "last_run_at": started.isoformat(timespec="seconds"),
                  "last_result": result})
    write_update_state(state)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
