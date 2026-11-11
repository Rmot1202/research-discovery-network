"""Daily OpenAlex import: add as many Computer Science papers as the free daily budget allows.

Free OpenAlex keys get $1 of API usage a day (resets at midnight UTC). A filtered list call
costs $0.0001 and returns up to 100 works, so one day of free budget can list ~1,000,000 works.
In practice the limit is the *database*, not OpenAlex: Neon's free plan holds 1 GB, so this
importer also stops at DB_STORAGE_LIMIT_MB and at OPENALEX_DAILY_MAX new papers per run.

`from_created_date` (the "new since" filter) is a paid OpenAlex feature, so the importer walks
by publication date instead, in two lanes:

1. recent   - re-checks the last OPENALEX_RECENT_DAYS days of publications (catches works that
              OpenAlex indexes a few days late; duplicates are skipped for free),
2. backfill - walks backward one publication day at a time toward OPENALEX_START_DATE, saving
              its position (day + cursor) so tomorrow's run continues where today's stopped.

Works that OpenAlex says are on arXiv are skipped: the arXiv importer already has them.
Affiliations come straight from OpenAlex authorships, so these papers need no PDF scan.

    python -m backend.openalex_import                 # one daily run (what the workflow calls)
    python -m backend.openalex_import --max-new 500   # small test run
    python -m backend.openalex_import --dry-run       # fetch one page and print what it would add

Environment:
    OPENALEX_API_KEY        required (free at https://openalex.org/settings/api); cursor paging needs it
    OPENALEX_DAILY_MAX      new papers per run (default 20000)
    OPENALEX_MINUTES        time cap per run (default 20)
    OPENALEX_RESERVE_CREDITS credits to leave for the affiliation scan (default 1000 = $0.10)
    OPENALEX_RECENT_DAYS    default 7
    OPENALEX_START_DATE     oldest publication day to backfill to (default 2015-01-01)
    OPENALEX_MIN_CITATIONS  optional: skip backfill works cited fewer times (default 0)
    DB_STORAGE_LIMIT_MB     stop before the database reaches this size (default 900)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backend import affiliation_scan, import_log
from backend import main as app_main
from backend.taxonomy import CATEGORY_CODES

WORKS_URL = "https://api.openalex.org/works"
PER_PAGE = 100  # OpenAlex maximum
USER_AGENT = "ResearchDiscoveryNetwork/0.3 (academic paper discovery; OpenAlex importer)"
SELECT = ",".join([
    "id", "doi", "display_name", "publication_date", "type", "language", "abstract_inverted_index",
    "authorships", "primary_topic", "topics", "keywords", "primary_location", "best_oa_location",
    "open_access", "cited_by_count", "indexed_in",
])

# OpenAlex subfields of field 17 (Computer Science) -> closest arXiv CS category.
SUBFIELD_TO_ARXIV = {
    "1700": "cs.OH",  # General Computer Science
    "1701": "cs.OH",  # Computer Science (miscellaneous)
    "1702": "cs.AI",  # Artificial Intelligence
    "1703": "cs.DS",  # Computational Theory and Mathematics
    "1704": "cs.GR",  # Computer Graphics and Computer-Aided Design
    "1705": "cs.NI",  # Computer Networks and Communications
    "1706": "cs.CY",  # Computer Science Applications
    "1707": "cs.CV",  # Computer Vision and Pattern Recognition
    "1708": "cs.AR",  # Hardware and Architecture
    "1709": "cs.HC",  # Human-Computer Interaction
    "1710": "cs.IR",  # Information Systems
    "1711": "cs.IT",  # Signal Processing
    "1712": "cs.SE",  # Software
}
# Topic-name keywords refine the subfield guess (checked in order, first match wins).
TOPIC_RULES: list[tuple[re.Pattern[str], str]] = [(re.compile(pattern, re.I), code) for pattern, code in [
    (r"secur|cryptograph|privacy|malware|intrusion|attack|blockchain|forensic", "cs.CR"),
    (r"natural language|language model|text|speech recogn|translation|sentiment|question answering", "cs.CL"),
    (r"robot|autonomous vehicle|navigation|manipulat|slam\b|drone|uav", "cs.RO"),
    (r"reinforcement|neural network|deep learning|machine learning|learning theory|federated", "cs.LG"),
    (r"image|video|vision|object detection|segmentation|face|pose estimation|point cloud", "cs.CV"),
    (r"database|query|data management|data stream|sql", "cs.DB"),
    (r"distributed|parallel|cloud|edge computing|high performance|gpu|scheduling", "cs.DC"),
    (r"wireless|network|internet of things|iot|5g|6g|routing|sdn", "cs.NI"),
    (r"software|program|code|testing|bug|compiler", "cs.SE"),
    (r"recommend|information retrieval|search engine|web mining", "cs.IR"),
    (r"social network|graph mining|community detection|misinformation", "cs.SI"),
    (r"game theory|auction|mechanism design", "cs.GT"),
    (r"evolutionary|genetic algorithm|swarm|neuromorphic|spiking", "cs.NE"),
    (r"multi-agent|multiagent", "cs.MA"),
    (r"education|ethic|society|health informatics|digital divide", "cs.CY"),
    (r"control system|power system|smart grid|fault diagnosis", "cs.SY"),
    (r"audio|music|sound", "cs.SD"),
    (r"quantum", "cs.ET"),
    (r"graph theory|combinator|algorithm|complexity", "cs.DS"),
    (r"logic|verification|formal method", "cs.LO"),
]]


class BudgetExhausted(RuntimeError):
    pass


def category_for_topic(topic: dict[str, Any] | None) -> str | None:
    if not topic:
        return None
    name = " ".join(filter(None, [topic.get("display_name"), (topic.get("subfield") or {}).get("display_name")]))
    for pattern, code in TOPIC_RULES:
        if pattern.search(name):
            return code
    subfield_id = str((topic.get("subfield") or {}).get("id", "")).rsplit("/", 1)[-1]
    return SUBFIELD_TO_ARXIV.get(subfield_id)


def categories_for_work(work: dict[str, Any]) -> list[str]:
    """Primary category first, then up to two more from secondary topics, all inside the scope."""
    found: list[str] = []
    for topic in [work.get("primary_topic"), *(work.get("topics") or [])[:3]]:
        code = category_for_topic(topic)
        if code and code in CATEGORY_CODES and code not in found:
            found.append(code)
    return found[:3]


def abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, places in index.items():
        for place in places:
            positions[place] = word
    return " ".join(positions[i] for i in sorted(positions))


def is_on_arxiv(work: dict[str, Any]) -> bool:
    if "arxiv" in (work.get("indexed_in") or []):
        return True
    doi = (work.get("doi") or "").lower()
    landing = ((work.get("primary_location") or {}).get("landing_page_url") or "").lower()
    return "10.48550/arxiv" in doi or "arxiv.org" in landing


def paper_from_work(work: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an OpenAlex work into the same paper shape the arXiv importer produces."""
    categories = categories_for_work(work)
    title = " ".join((work.get("display_name") or "").split())
    abstract = " ".join(abstract_from_inverted_index(work.get("abstract_inverted_index")).split())
    if not categories or not title or len(abstract) < 200 or is_on_arxiv(work):
        return None
    work_id = str(work.get("id", "")).rsplit("/", 1)[-1]
    if not re.fullmatch(r"W\d+", work_id):
        return None
    location = work.get("primary_location") or {}
    oa = work.get("best_oa_location") or {}
    doi = work.get("doi")
    authors = [
        (authorship.get("author") or {}).get("display_name") or authorship.get("raw_author_name") or ""
        for authorship in work.get("authorships") or []
    ]
    published = work.get("publication_date") or ""
    return {
        "arxiv_id": work_id,  # stored in the shared ID column; see arxiv_utils.is_openalex_id
        "source": "openalex",
        "openalex_id": work.get("id"),
        "doi": doi,
        "url": doi or location.get("landing_page_url") or work.get("id"),
        "pdf_url": oa.get("pdf_url") or oa.get("landing_page_url"),
        "venue": (location.get("source") or {}).get("display_name"),
        "work_type": work.get("type"),
        "is_open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "cited_by_count": work.get("cited_by_count") or 0,
        "openalex_topic": (work.get("primary_topic") or {}).get("display_name"),
        "keywords": [k.get("display_name") for k in (work.get("keywords") or [])[:8] if k.get("display_name")],
        "title": title,
        "abstract": abstract,
        "authors": [name for name in authors if name][:50],
        "published_at": f"{published}T00:00:00Z" if published else "",
        "categories": categories,
        "primary_category": categories[0],
        "ingested_at": datetime.now(UTC).isoformat(),
        "affiliation": None,
        "institution": None,
        "country": None,
        "affiliation_source": None,
    }


class OpenAlexClient:
    def __init__(self, api_key: str, reserve_credits: int) -> None:
        self.api_key = api_key
        self.reserve = reserve_credits
        self.remaining: float | None = None
        self.credits_used = 0.0
        self.calls = 0

    def list_works(self, filters: str, cursor: str) -> tuple[list[dict[str, Any]], str | None]:
        if self.remaining is not None and self.remaining <= self.reserve:
            raise BudgetExhausted(f"stopping with {self.remaining:.0f} credits left (reserve {self.reserve})")
        params = {"filter": filters, "per-page": str(PER_PAGE), "cursor": cursor, "select": SELECT,
                  "sort": "publication_date:desc"}
        request = urllib.request.Request(
            f"{WORKS_URL}?{urllib.parse.urlencode(params, safe=':,|*')}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    self._track(response.headers)
                self.calls += 1
                return body.get("results") or [], (body.get("meta") or {}).get("next_cursor")
            except urllib.error.HTTPError as error:
                text_body = error.read().decode("utf-8", "replace")
                if error.code == 429 and ("budget" in text_body.lower() or "plan upgrade" in text_body.lower()):
                    raise BudgetExhausted(text_body[:300]) from error
                if error.code in (429, 500, 502, 503, 504) and attempt < 4:
                    retry = error.headers.get("Retry-After")
                    time.sleep(min(float(retry), 60) if retry and retry.isdigit() else 2 ** attempt * 2)
                    continue
                raise RuntimeError(f"OpenAlex returned {error.code}: {text_body[:300]}") from error
            except (urllib.error.URLError, TimeoutError):
                if attempt < 4:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
        raise RuntimeError("unreachable")

    def _track(self, headers: Any) -> None:
        try:
            if headers.get("X-RateLimit-Remaining") is not None:
                self.remaining = float(headers["X-RateLimit-Remaining"])
            if headers.get("X-RateLimit-Credits-Used") is not None:
                self.credits_used += float(headers["X-RateLimit-Credits-Used"])
        except (TypeError, ValueError):
            pass


def base_filter(day_from: str, day_to: str, min_citations: int = 0) -> str:
    parts = [
        "primary_topic.field.id:17",           # Computer Science
        "has_abstract:true",
        "type:article|preprint|review",
        "is_retracted:false",
        "language:en",
        f"from_publication_date:{day_from}",
        f"to_publication_date:{day_to}",
    ]
    if min_citations > 0:
        parts.append(f"cited_by_count:>{min_citations - 1}")
    return ",".join(parts)


STATE_TABLE_SQL = "CREATE TABLE IF NOT EXISTS openalex_import_state (id INTEGER PRIMARY KEY, state_json TEXT NOT NULL)"


def _read_state() -> dict[str, Any]:
    with app_main.connect_db() as connection:
        connection.execute(STATE_TABLE_SQL)
        row = connection.execute("SELECT state_json FROM openalex_import_state WHERE id = 1").fetchone()
    return json.loads(row["state_json"]) if row else {}


def _write_state(state: dict[str, Any]) -> None:
    with app_main.connect_db() as connection:
        connection.execute(STATE_TABLE_SQL)
        connection.execute(
            "INSERT INTO openalex_import_state (id, state_json) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
            (json.dumps(state),),
        )


def save_page(works: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert one page of works; returns (new papers, affiliations saved)."""
    papers = [paper for paper in (paper_from_work(work) for work in works) if paper]
    if not papers:
        return 0, 0
    inserted = app_main.insert_papers(papers)
    if not inserted:
        return 0, 0
    with_affiliations = 0
    by_id = {str(work.get("id", "")).rsplit("/", 1)[-1]: work for work in works}
    for paper in papers:
        work = by_id.get(paper["arxiv_id"])
        affiliations = affiliation_scan.affiliations_from_openalex(work) if work else []
        if affiliations and affiliation_scan.save_affiliations(
            app_main.ENGINE, paper["arxiv_id"], affiliations, "openalex", work["id"],
        ):
            with_affiliations += 1
    return inserted, with_affiliations


def run(max_new: int, max_minutes: float, dry_run: bool = False) -> dict[str, Any]:
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Set OPENALEX_API_KEY (free at https://openalex.org/settings/api); cursor paging needs a key.")
    client = OpenAlexClient(api_key, int(os.environ.get("OPENALEX_RESERVE_CREDITS", "1000")))
    started = time.monotonic()
    deadline = started + max_minutes * 60
    today = date.today()
    recent_days = int(os.environ.get("OPENALEX_RECENT_DAYS", "7"))
    start_date = date.fromisoformat(os.environ.get("OPENALEX_START_DATE", "2015-01-01"))
    min_citations = int(os.environ.get("OPENALEX_MIN_CITATIONS", "0"))
    state = _read_state()
    result: dict[str, Any] = {"new_papers": 0, "with_affiliations": 0, "pages": 0, "stopped": "finished"}

    def budget_left() -> str | None:
        if result["new_papers"] >= max_new:
            return "daily_max"
        if time.monotonic() > deadline:
            return "time_limit"
        if result["pages"] % 20 == 0 and not import_log.storage_ok(app_main.ENGINE):
            return "storage_limit"
        return None

    def harvest(filters: str, cursor: str, on_page: Any = None) -> tuple[str | None, str | None]:
        """Page through one filter; returns (stop reason or None, cursor to resume from)."""
        while cursor:
            reason = budget_left()
            if reason:
                return reason, cursor
            works, next_cursor = client.list_works(filters, cursor)
            result["pages"] += 1
            if dry_run:
                preview = [paper_from_work(work) for work in works]
                result["preview"] = [
                    {"id": p["arxiv_id"], "category": p["primary_category"], "title": p["title"][:90]}
                    for p in preview if p][:10]
                result["skipped_on_page"] = sum(1 for p in preview if not p)
                return "dry_run", None
            new, affiliated = save_page(works)
            result["new_papers"] += new
            result["with_affiliations"] += affiliated
            if on_page:
                on_page(next_cursor)
            if result["pages"] % 10 == 0:
                print(f"  {result['pages']} pages, {result['new_papers']:,} new, "
                      f"{client.remaining if client.remaining is not None else '?'} credits left",
                      file=sys.stderr, flush=True)
            if not works:
                return None, None
            cursor = next_cursor
        return None, None

    try:
        # Lane 1: recent publications.
        recent_from = (today - timedelta(days=recent_days)).isoformat()
        print(f"OpenAlex recent lane: published {recent_from}..{today}", file=sys.stderr, flush=True)
        reason, _ = harvest(base_filter(recent_from, today.isoformat()), "*")
        # Lane 2: backfill, one publication day at a time, newest first.
        if not reason:
            day = date.fromisoformat(state.get("backfill_day") or (today - timedelta(days=recent_days + 1)).isoformat())
            cursor = state.get("backfill_cursor") or "*"
            print(f"OpenAlex backfill lane: from {day} back to {start_date}", file=sys.stderr, flush=True)
            while day >= start_date and not reason:
                def remember(next_cursor: str | None, day: date = day) -> None:
                    state.update({"backfill_day": day.isoformat(), "backfill_cursor": next_cursor or ""})
                    _write_state(state)
                reason, cursor = harvest(base_filter(day.isoformat(), day.isoformat(), min_citations), cursor, remember)
                if reason:
                    break
                day -= timedelta(days=1)
                cursor = "*"
                state.update({"backfill_day": day.isoformat(), "backfill_cursor": ""})
                _write_state(state)
            if day < start_date:
                reason = "backfill_complete"
        result["stopped"] = reason or "finished"
    except BudgetExhausted as error:
        result["stopped"] = "openalex_budget"
        result["note"] = str(error)[:300]
    result.update({
        "api_calls": client.calls,
        "credits_used": round(client.credits_used, 1),
        "credits_left": client.remaining,
        "backfill_day": state.get("backfill_day"),
        "database_mb": import_log.database_size_mb(app_main.ENGINE),
        "minutes": round((time.monotonic() - started) / 60, 1),
    })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-new", type=int, default=int(os.environ.get("OPENALEX_DAILY_MAX", "20000")))
    parser.add_argument("--max-minutes", type=float, default=float(os.environ.get("OPENALEX_MINUTES", "20")))
    parser.add_argument("--dry-run", action="store_true", help="Fetch one page and print what would be added.")
    args = parser.parse_args()
    started_at = import_log.now_iso()
    result = run(args.max_new, args.max_minutes, args.dry_run)
    if not args.dry_run:
        import_log.log_run(app_main.ENGINE, "openalex", started_at, result["new_papers"], result)
        affiliation_scan._invalidate_gap_cache()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
