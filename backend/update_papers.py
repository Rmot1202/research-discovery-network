"""Daily update: import papers arXiv added since the last run, then look up their affiliations.

Affiliations come from the papers' own PDFs by default (free, no account). Set
AFFILIATION_SOURCES=openalex+pdf to also use OpenAlex.

Run by the scheduled GitHub Actions workflow (.github/workflows/update-papers.yml) against the
hosted database, or by hand:

    python -m backend.update_papers                  # since the last run (first run: last 3 days)
    python -m backend.update_papers --since 2026-10-01
    python -m backend.update_papers --no-affiliations
    python -m backend.update_papers --backfill-pages 15   # also walk older arXiv weeks (resumable)

The backfill lane replaces the old "import locally, then copy to Render" step: each daily run
harvests a few more older weeks straight into the hosted database and remembers where it stopped.

New papers are always added. Whether they appear in the homepage "Featured" list right away
depends on PUBLISH_MODE on the web service (auto = yes, review = after approval).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from backend import affiliation_scan, import_log, main as app_main
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


BACKFILL_WINDOW_DAYS = 7
BACKFILL_OLDEST = "2005-01-01"  # arXiv OAI datestamps start in the 2000s, even for older papers


def backfill_arxiv(state: dict[str, Any], max_pages: int) -> dict[str, Any]:
    """Harvest older arXiv CS records in one-week datestamp windows, newest week first.

    Every record has exactly one OAI datestamp, so walking the windows backward eventually covers
    the whole archive. Progress (window + resumption token) is saved after every page; if a saved
    token has expired, the window simply restarts (duplicates are ignored)."""
    oldest = date.fromisoformat(os.environ.get("ARXIV_BACKFILL_OLDEST", BACKFILL_OLDEST))
    until = date.fromisoformat(state.get("backfill_until") or (date.today() - timedelta(days=FIRST_RUN_DAYS + 1)).isoformat())
    token: str | None = state.get("backfill_token") or None
    pages = inserted = 0
    stopped = "page_limit"
    while pages < max_pages:
        if until < oldest:
            stopped = "complete"
            break
        if pages % 5 == 0 and not import_log.storage_ok(app_main.ENGINE):
            stopped = "storage_limit"
            break
        window_from = until - timedelta(days=BACKFILL_WINDOW_DAYS - 1)
        try:
            papers, token, _ = fetch_oai_page(token, None if token else window_from.isoformat(),
                                              None if token else until.isoformat())
        except RuntimeError as error:
            if token:  # expired resumption token: restart this window next time round
                token = None
                state["backfill_token"] = None
                continue
            stopped = f"error: {error}"[:200]
            break
        pages += 1
        inserted += insert_papers(papers)
        if not token:  # window finished: step back one week
            until = window_from - timedelta(days=1)
        state.update({"backfill_until": until.isoformat(), "backfill_token": token})
        write_update_state(state)
        print(f"  backfill page {pages}: window ending {until}, {inserted:,} new so far", file=sys.stderr, flush=True)
        time.sleep(OAI_REQUEST_INTERVAL)
    return {"backfill_pages": pages, "backfill_new_papers": inserted, "backfill_until": until.isoformat(),
            "backfill_stopped": stopped}


def enrich_recent_pdf(days: int = 14, max_papers: int | None = None) -> dict[str, int]:
    """Read the PDFs of recent papers that have no affiliation yet (newest first, capped per run)."""
    max_papers = max_papers if max_papers is not None else int(os.environ.get("AFFILIATION_DAILY_LIMIT", "300"))
    since = (date.today() - timedelta(days=days)).isoformat()
    with app_main.ENGINE.connect() as connection:
        ids = [row[0] for row in connection.execute(
            text("SELECT p.arxiv_id FROM papers p WHERE p.institution IS NULL AND p.arxiv_id NOT LIKE 'W%' AND p.published_at >= :s "
                 "ORDER BY p.published_at DESC LIMIT :n"),
            {"s": since, "n": max_papers * 3},
        ).all()]
    with app_main.ENGINE.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS affiliation_pdf_checked (arxiv_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL)"
        ))
        checked = {row[0] for row in connection.execute(text("SELECT arxiv_id FROM affiliation_pdf_checked")).all()}
    todo = [arxiv_id for arxiv_id in ids if arxiv_id not in checked][:max_papers]
    return affiliation_scan.scan_pdfs_for(app_main.ENGINE, todo, log=lambda m: print(m, file=sys.stderr, flush=True))


def enrich_recent(days: int = 60, max_papers: int = 5000) -> dict[str, int]:
    """OpenAlex affiliations for recent papers that still have none.

    New arXiv papers usually reach OpenAlex within days, so recent papers are re-checked on
    each run until they're found or older than `days`.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    with app_main.ENGINE.connect() as connection:
        ids = [row[0] for row in connection.execute(
            text("SELECT arxiv_id FROM papers WHERE institution IS NULL AND arxiv_id NOT LIKE 'W%' AND published_at >= :s "
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
    parser.add_argument("--no-affiliations", action="store_true", help="Skip the affiliation lookup.")
    parser.add_argument("--affiliations-only", action="store_true",
                        help="Only look up affiliations for recent papers (no import).")
    parser.add_argument("--backfill-pages", type=int, default=int(os.environ.get("ARXIV_BACKFILL_PAGES", "0")),
                        help="Also harvest this many pages (~1,000 records each) of older arXiv weeks.")
    args = parser.parse_args()
    started_at = import_log.now_iso()

    if args.affiliations_only:
        use_openalex, use_pdf = affiliation_scan.configured_sources()
        found: dict[str, Any] = {}
        if use_openalex:
            found.update(enrich_recent())
        if use_pdf:
            found.update(enrich_recent_pdf())
        print(json.dumps(found, indent=2))
        return

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
    if args.backfill_pages > 0:
        print(f"Backfilling older arXiv weeks (up to {args.backfill_pages} pages)...", file=sys.stderr, flush=True)
        result.update(backfill_arxiv(state, args.backfill_pages))
    if not args.no_affiliations:
        use_openalex, use_pdf = affiliation_scan.configured_sources()
        print("Looking up affiliations for recent papers...", file=sys.stderr, flush=True)
        if use_openalex:
            result.update(enrich_recent())
        if use_pdf:
            result.update(enrich_recent_pdf())
    with connect_db() as connection:
        result["total_papers"] = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    state.update({"last_run_date": started.date().isoformat(), "last_run_at": started.isoformat(timespec="seconds"),
                  "last_result": result})
    write_update_state(state)
    import_log.log_run(app_main.ENGINE, "arxiv", started_at,
                       result["new_papers"] + result.get("backfill_new_papers", 0), result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
