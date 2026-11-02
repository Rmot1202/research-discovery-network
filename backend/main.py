from __future__ import annotations

import asyncio
import json
import os
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from backend.ai_service import (
    extract_pdf_affiliations, generate_embeddings, generate_research_analysis,
)
from backend.arxiv_utils import normalize_arxiv_id
from backend.database import create_database_engine, database_connection
from backend.paper_queries import (
    get_filter_options as query_filter_options,
    read_paper_rows as query_paper_rows,
    serialize_paper,
)
from backend.paper_chat import (
    ai_chat_configured, answer_question, collection_stats, ensure_search_index, retrieve_papers,
)
from backend import affiliation_scan
from backend.research_gaps import gap_summary_for_chat, idea_gap_report, invalidate_cache, topic_gap_report
from backend.schemas import ChatRequest, DocumentedAffiliation, ReviewUpdate
from backend.security import require_admin
from backend.taxonomy import CATEGORIES, CATEGORY_CODES, CATEGORY_LABELS


APP_DIR = Path(__file__).resolve().parent
ENGINE = create_database_engine()
LEGACY_FILE = Path(os.environ.get("PAPERS_FILE", APP_DIR / "data" / "papers.json"))
ARXIV_OAI = "https://oaipmh.arxiv.org/oai"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
ARXIV_METADATA = "{http://arxiv.org/OAI/arXiv/}"
OAI_REQUEST_INTERVAL = 20
# "review": only editor-approved papers are public (URD spec default).
# "auto": every paper that isn't rejected is public right away; approving just features it.
PUBLISH_MODE = "auto" if os.environ.get("PUBLISH_MODE", "review").strip().lower() == "auto" else "review"


def public_approval_filter() -> str:
    return "public" if PUBLISH_MODE == "auto" else "approved"


app = FastAPI(title="Research Discovery API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8443",
        "http://127.0.0.1:8443",
        # Extra origins for a separately hosted frontend, comma-separated (e.g. a GitHub Pages URL).
        *[origin.strip() for origin in os.environ.get("CORS_ORIGINS", "").split(",") if origin.strip()],
    ],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)

_ingest_task: asyncio.Task[None] | None = None


def connect_db() -> Any:
    return database_connection(ENGINE)


def init_db() -> None:
    with connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS papers (
                arxiv_id TEXT PRIMARY KEY,
                raw_metadata TEXT NOT NULL,
                analysis TEXT NOT NULL,
                primary_category TEXT NOT NULL,
                title TEXT NOT NULL,
                abstract TEXT NOT NULL,
                authors TEXT NOT NULL,
                institution TEXT,
                country TEXT,
                published_at TEXT NOT NULL,
                approval_status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_category ON papers(primary_category)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_country ON papers(country)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_institution ON papers(institution)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_categories (
                arxiv_id TEXT NOT NULL,
                category TEXT NOT NULL,
                PRIMARY KEY (arxiv_id, category)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_categories_category ON paper_categories(category)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ingest_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL
            )
            """
        )
    migrate_legacy_json()


def migrate_legacy_json() -> None:
    if not LEGACY_FILE.exists():
        return
    try:
        legacy = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read existing paper data at {LEGACY_FILE}: {error}") from error

    rows = []
    for entry in legacy:
        raw = entry.get("raw_metadata", entry)
        analysis = entry.get("analysis", entry)
        arxiv_id = raw.get("arxiv_id")
        if not arxiv_id:
            continue
        categories = raw.get("categories", [])
        primary = next((code for code in categories if code in CATEGORY_CODES), "")
        if not primary:  # outside the research scope (see backend/taxonomy.py)
            continue
        rows.append(
            (
                arxiv_id,
                json.dumps(raw, ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False),
                primary,
                raw.get("title", ""),
                raw.get("abstract", ""),
                json.dumps(raw.get("authors", []), ensure_ascii=False),
                raw.get("institution"),
                raw.get("country"),
                raw.get("published_at", ""),
                analysis.get("approval_status", "pending"),
            )
        )
    if rows:
        with connect_db() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO papers (
                    arxiv_id, raw_metadata, analysis, primary_category, title,
                    abstract, authors, institution, country, published_at, approval_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    with connect_db() as connection:
        category_count = connection.execute("SELECT COUNT(*) FROM paper_categories").fetchone()[0]
        if category_count == 0:
            paper_rows = connection.execute(
                "SELECT arxiv_id, raw_metadata FROM papers"
            ).fetchall()
            memberships = []
            for paper in paper_rows:
                try:
                    categories = json.loads(paper["raw_metadata"]).get("categories", [])
                except (json.JSONDecodeError, TypeError):
                    continue
                memberships.extend(
                    (paper["arxiv_id"], category)
                    for category in categories
                    if category in CATEGORY_CODES
                )
            if memberships:
                connection.executemany(
                    "INSERT OR IGNORE INTO paper_categories (arxiv_id, category) VALUES (?, ?)",
                    memberships,
                )


def normalize_documented_value(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def parse_feed(feed: bytes) -> tuple[list[dict[str, Any]], str | None, int]:
    root = ET.fromstring(feed)
    page = root.find(f"{OAI}ListRecords")
    if page is None:
        error = root.find(f"{OAI}error")
        message = error.text if error is not None else "Unexpected response from arXiv OAI-PMH"
        raise ValueError(message)
    token_element = page.find(f"{OAI}resumptionToken")
    token = (
        urllib.parse.unquote(token_element.text.strip())
        if token_element is not None and token_element.text
        else None
    )
    total_results = (
        int(token_element.attrib.get("completeListSize", "0"))
        if token_element is not None
        else 0
    )
    papers = []
    for record in page.findall(f"{OAI}record"):
        metadata = record.find(f"{OAI}metadata/{ARXIV_METADATA}arXiv")
        if metadata is None:
            continue
        identifier = metadata.findtext(f"{ARXIV_METADATA}id", default="")
        if not identifier:
            identifier = record.findtext(f"{OAI}header/{OAI}identifier", default="")
            identifier = identifier.rsplit(":", 1)[-1]
        arxiv_id = normalize_arxiv_id(identifier)
        if not arxiv_id:
            continue
        categories = metadata.findtext(f"{ARXIV_METADATA}categories", default="").split()
        primary_category = next(
            (category for category in categories if category in CATEGORY_CODES),
            "",
        )
        created = metadata.findtext(f"{ARXIV_METADATA}created", default="")
        published = f"{created}T00:00:00Z" if created else ""
        authors = []
        author_list = metadata.find(f"{ARXIV_METADATA}authors")
        if author_list is not None:
            for author in author_list.findall(f"{ARXIV_METADATA}author"):
                name = " ".join(
                    part
                    for part in (
                        author.findtext(f"{ARXIV_METADATA}forenames", default="").strip(),
                        author.findtext(f"{ARXIV_METADATA}keyname", default="").strip(),
                    )
                    if part
                )
                if name:
                    authors.append(name)
        title = " ".join(metadata.findtext(f"{ARXIV_METADATA}title", default="").split())
        abstract = " ".join(metadata.findtext(f"{ARXIV_METADATA}abstract", default="").split())
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "published_at": published,
                "categories": categories,
                "primary_category": primary_category,
                "ingested_at": datetime.now(UTC).isoformat(),
                "affiliation": None,
                "institution": None,
                "country": None,
                "affiliation_source": None,
            }
        )
    return papers, token, total_results


def fetch_oai_page(
    token: str | None, from_date: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, int]:
    params = {"verb": "ListRecords", "metadataPrefix": "arXiv", "set": "cs"}
    if from_date:  # incremental harvest: records added or changed on/after this day (YYYY-MM-DD)
        params["from"] = from_date
    if token:
        params = {"verb": "ListRecords", "resumptionToken": token}
    request = urllib.request.Request(
        f"{ARXIV_OAI}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": "ResearchDiscoveryMVP/0.2 (metadata-only academic discovery prototype)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        if from_date and not token and b"noRecordsMatch" in body:
            return [], None, 0
        return parse_feed(body)
    except (OSError, ET.ParseError, ValueError) as error:
        raise RuntimeError(f"Could not retrieve arXiv Computer Science metadata: {error}") from error


def insert_papers(papers: list[dict[str, Any]]) -> int:
    inserted = 0
    with connect_db() as connection:
        for paper in papers:
            category = paper["primary_category"]
            if not category:  # no category inside the research scope (backend/taxonomy.py)
                continue
            analysis = {
                "topic": CATEGORY_LABELS.get(category, category),
                "cluster_id": None,
                "representation_score": 0,
                "score_components": {
                    "topic_rarity": None,
                    "institution_visibility": None,
                    "geographic_visibility": None,
                },
                "summary": None,
                "approval_status": "pending",
            }
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO papers (
                    arxiv_id, raw_metadata, analysis, primary_category, title,
                    abstract, authors, institution, country, published_at, approval_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    paper["arxiv_id"],
                    json.dumps(paper, ensure_ascii=False),
                    json.dumps(analysis, ensure_ascii=False),
                    category,
                    paper["title"],
                    paper["abstract"],
                    json.dumps(paper["authors"], ensure_ascii=False),
                    None,
                    None,
                    paper["published_at"],
                ),
            )
            inserted += cursor.rowcount
            connection.executemany(
                "INSERT OR IGNORE INTO paper_categories (arxiv_id, category) VALUES (?, ?)",
                [
                    (paper["arxiv_id"], category)
                    for category in paper["categories"]
                    if category in CATEGORY_CODES
                ],
            )
    return inserted


def default_ingest_state() -> dict[str, Any]:
    return {
        "status": "not_started",
        "total_records": 0,
        "records_processed": 0,
        "pages_processed": 0,
        "ingested_papers": 0,
        "resumption_token": None,
        "error": None,
        "updated_at": None,
    }


def read_ingest_state() -> dict[str, Any]:
    with connect_db() as connection:
        row = connection.execute("SELECT state_json FROM ingest_state WHERE id = 1").fetchone()
    if not row:
        return default_ingest_state()
    return {**default_ingest_state(), **json.loads(row["state_json"])}


def write_ingest_state(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO ingest_state (id, state_json) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json
            """,
            (json.dumps(state),),
        )


def read_paper_rows(
    *,
    query: str,
    category: str | None,
    country: str | None,
    institution: str | None,
    year: int | None,
    sort: str,
    limit: int,
    offset: int,
    approval_status: str | None = None,
) -> tuple[list[Any], int, dict[str, int], dict[str, int], dict[str, int]]:
    return query_paper_rows(
        connect_db, query=query, category=category, country=country, institution=institution,
        year=year, sort=sort, limit=limit, offset=offset, approval_status=approval_status,
    )


def get_filter_options() -> dict[str, Any]:
    return query_filter_options(connect_db)


async def run_full_ingest() -> None:
    state = read_ingest_state()
    state["status"] = "running"
    state["error"] = None
    write_ingest_state(state)
    try:
        while True:
            state = read_ingest_state()
            if state["status"] == "paused":
                return
            papers, next_token, total_records = await asyncio.to_thread(
                fetch_oai_page, state["resumption_token"]
            )
            inserted = await asyncio.to_thread(insert_papers, papers)
            state = read_ingest_state()
            state["pages_processed"] += 1
            state["records_processed"] += len(papers)
            if total_records:
                state["total_records"] = total_records
            state["ingested_papers"] += inserted
            state["resumption_token"] = next_token
            if not next_token:
                state["status"] = "complete"
            write_ingest_state(state)
            if not next_token:
                return
            await asyncio.sleep(OAI_REQUEST_INTERVAL)
        state = read_ingest_state()
        state["status"] = "complete"
        write_ingest_state(state)
    except Exception as error:
        state = read_ingest_state()
        state["status"] = "error"
        state["error"] = str(error)
        write_ingest_state(state)


@app.on_event("startup")
def startup() -> None:
    init_db()
    state = read_ingest_state()
    if state["status"] == "running":
        state["status"] = "paused"
        state["error"] = "API restarted; resume the full import to continue from its saved cursor."
        write_ingest_state(state)
    import threading

    def build_index() -> None:
        try:
            ensure_search_index(ENGINE, force=True)
        except Exception as error:  # the assistant falls back to rebuilding on first question
            print(f"Research assistant search index build skipped: {error}")

    threading.Thread(target=build_index, daemon=True).start()
    affiliation_scan.mark_interrupted(ENGINE)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/topics")
def list_topics() -> dict[str, Any]:
    options = get_filter_options()
    topics = [
        {
            "code": code,
            "label": label,
            "count": options["counts"].get(code, 0),
        }
        for code, label in CATEGORIES
    ]
    with connect_db() as connection:
        total = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    return {"topics": topics, "total_papers": total}


@app.get("/api/topic-links")
def list_topic_links() -> dict[str, Any]:
    categories = tuple(CATEGORY_CODES)
    placeholders = ",".join("?" for _ in categories)
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT left_category.category AS source, "
            "right_category.category AS target, "
            "COUNT(*) AS shared_papers "
            "FROM paper_categories AS left_category "
            "JOIN paper_categories AS right_category "
            "ON left_category.arxiv_id = right_category.arxiv_id "
            "AND left_category.category < right_category.category "
            f"WHERE left_category.category IN ({placeholders}) "
            f"AND right_category.category IN ({placeholders}) "
            "GROUP BY left_category.category, right_category.category",
            categories + categories,
        ).fetchall()

    links = [
        {
            "source": row["source"],
            "target": row["target"],
            "shared_papers": row["shared_papers"],
        }
        for row in rows
    ]
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        by_topic.setdefault(link["source"], []).append(link)
        by_topic.setdefault(link["target"], []).append(link)

    visible_keys = set()
    for topic_links in by_topic.values():
        strongest = sorted(
            topic_links,
            key=lambda link: (-link["shared_papers"], link["source"], link["target"]),
        )[:4]
        visible_keys.update((link["source"], link["target"]) for link in strongest)

    visible_links = [
        link for link in links
        if (link["source"], link["target"]) in visible_keys
    ]
    visible_links.sort(key=lambda link: (link["shared_papers"], link["source"], link["target"]))
    return {"links": visible_links, "available_links": len(links)}


@app.get("/api/filters")
def list_filter_options() -> dict[str, Any]:
    options = get_filter_options()
    return {
        "institutions": options["institutions"],
        "countries": options["countries"],
        "years": options["years"],
        "topics": [
            {"code": code, "label": label, "count": options["counts"].get(code, 0)}
            for code, label in CATEGORIES
        ],
    }


@app.get("/api/papers")
def list_papers(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default="", max_length=250),
    category: str | None = None,
    country: str | None = None,
    institution: str | None = None,
    year: int | None = Query(default=None, ge=1991, le=2100),
    sort: str = "newest",
) -> dict[str, Any]:
    rows, total, category_counts, institution_counts, country_counts = read_paper_rows(
        query=q,
        category=category,
        country=country,
        institution=institution,
        year=year,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    with connect_db() as connection:
        all_count = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    return {
        "count": all_count,
        "total": total,
        "limit": limit,
        "offset": offset,
        "papers": [
            serialize_paper(row, category_counts, institution_counts, country_counts, all_count)
            for row in rows
        ],
    }


@app.get("/api/featured")
def list_featured_papers(
    limit: int = Query(default=12, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default="", max_length=250),
    category: str | None = None,
    country: str | None = None,
    institution: str | None = None,
    year: int | None = Query(default=None, ge=1991, le=2100),
    sort: str = "newest",
) -> dict[str, Any]:
    rows, total, category_counts, institution_counts, country_counts = read_paper_rows(
        query=q,
        category=category,
        country=country,
        institution=institution,
        year=year,
        sort="featured" if PUBLISH_MODE == "auto" and sort == "newest" else sort,
        limit=limit,
        offset=offset,
        approval_status=public_approval_filter(),
    )
    with connect_db() as connection:
        all_count = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    return {
        "total": total,
        "publish_mode": PUBLISH_MODE,
        "limit": limit,
        "offset": offset,
        "papers": [
            serialize_paper(row, category_counts, institution_counts, country_counts, all_count)
            for row in rows
        ],
    }


@app.get("/api/gaps/topics")
async def research_gaps_by_topic() -> dict[str, Any]:
    return await asyncio.to_thread(topic_gap_report, ENGINE)


@app.get("/api/gaps/idea")
async def research_gap_for_idea(
    q: str = Query(min_length=2, max_length=250),
    category: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(idea_gap_report, ENGINE, q, category or None)


@app.get("/api/affiliations/progress")
def affiliation_scan_progress() -> dict[str, Any]:
    return affiliation_scan.progress(ENGINE)


@app.post("/api/admin/affiliations/scan")
def start_affiliation_scan(
    pdf_fallback: bool = False,
    limit: int | None = Query(default=None, ge=1, le=1_000_000),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    return affiliation_scan.start_background(ENGINE, pdf_fallback=pdf_fallback, limit=limit)


@app.post("/api/admin/affiliations/pause")
def pause_affiliation_scan(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    return affiliation_scan.pause_background(ENGINE)


@app.get("/api/chat/status")
def chat_status() -> dict[str, Any]:
    return {
        "ai_configured": ai_chat_configured(),
        "chat_model": os.environ.get("AI_CHAT_MODEL") or None,
        "semantic_rerank": bool(os.environ.get("AI_EMBEDDING_MODEL")),
    }


@app.post("/api/chat")
async def chat_with_papers(request: ChatRequest) -> dict[str, Any]:
    papers, method = await asyncio.to_thread(
        retrieve_papers, ENGINE, request.question, category=request.category, limit=request.limit,
    )
    if not ai_chat_configured():
        return {
            "answer": None,
            "papers": papers,
            "retrieval": method,
            "ai_configured": False,
            "notice": "The AI model is not configured yet, so these are the best-matching papers from the archive. "
            "Set AI_BASE_URL and AI_CHAT_MODEL in .env to get written answers.",
        }
    stats = await asyncio.to_thread(collection_stats, connect_db)
    stats["research_gap_scores"] = await asyncio.to_thread(gap_summary_for_chat, ENGINE)
    answer = await asyncio.to_thread(
        answer_question,
        request.question,
        [turn.model_dump() for turn in request.history],
        papers,
        stats,
        request.category,
    )
    return {"answer": answer, "papers": papers, "retrieval": method, "ai_configured": True}


@app.get("/api/admin/overview")
def admin_overview(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    with connect_db() as connection:
        counts = connection.execute(
            """
            SELECT COUNT(*) AS total,
                SUM(CASE WHEN approval_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN approval_status = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN approval_status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN institution IS NOT NULL THEN 1 ELSE 0 END) AS documented_institutions,
                SUM(CASE WHEN country IS NOT NULL THEN 1 ELSE 0 END) AS documented_countries
            FROM papers
            """
        ).fetchone()
        analyzed = sum(
            1
            for row in connection.execute("SELECT analysis FROM papers")
            if json.loads(row["analysis"]).get("embedding")
        )
    return {
        **dict(counts._mapping),
        "publish_mode": PUBLISH_MODE,
        "embedded": analyzed,
        "ai_configured": bool(
            os.environ.get("AI_BASE_URL")
            and os.environ.get("AI_EMBEDDING_MODEL")
            and os.environ.get("AI_CHAT_MODEL")
            and (
                os.environ.get("AI_API_KEY")
                or urllib.parse.urlparse(os.environ.get("AI_BASE_URL", "")).hostname
                in {"localhost", "127.0.0.1", "::1"}
            )
        ),
    }


@app.get("/api/admin/reviews")
def admin_review_queue(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    rows, total, category_counts, institution_counts, country_counts = read_paper_rows(
        query="",
        category=None,
        country=None,
        institution=None,
        year=None,
        sort="score",
        limit=limit,
        offset=offset,
        approval_status="pending",
    )
    with connect_db() as connection:
        all_count = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    papers = [
        serialize_paper(row, category_counts, institution_counts, country_counts, all_count)
        for row in rows
    ]
    papers.sort(key=lambda paper: paper["representation_score"], reverse=True)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "papers": papers,
    }


@app.put("/api/admin/papers/{arxiv_id}/review")
def update_paper_review(
    arxiv_id: str,
    update: ReviewUpdate,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    changes = update.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide an approval decision or summary field to update.")
    with connect_db() as connection:
        row = connection.execute("SELECT analysis, approval_status FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        analysis = json.loads(row["analysis"])
        status = changes.pop("approval_status", None)
        for field, value in changes.items():
            analysis[field] = value.strip() if value is not None else None
        if status is not None:
            analysis["approval_status"] = status
            connection.execute(
                "UPDATE papers SET analysis = ?, approval_status = ? WHERE arxiv_id = ?",
                (json.dumps(analysis, ensure_ascii=False), status, arxiv_id),
            )
        else:
            connection.execute(
                "UPDATE papers SET analysis = ? WHERE arxiv_id = ?",
                (json.dumps(analysis, ensure_ascii=False), arxiv_id),
            )
    return {"arxiv_id": arxiv_id, "approval_status": status or row["approval_status"], **analysis}


@app.put("/api/admin/papers/{arxiv_id}/affiliation")
def update_documented_affiliation(
    arxiv_id: str,
    affiliation: DocumentedAffiliation,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    if not affiliation.source_url.startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="The affiliation source must be an HTTP or HTTPS URL.")
    institution = normalize_documented_value(affiliation.institution)
    country = normalize_documented_value(affiliation.country) if affiliation.country else None
    evidence = " ".join(affiliation.evidence.split())
    with connect_db() as connection:
        row = connection.execute("SELECT analysis FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        analysis = json.loads(row["analysis"])
        analysis["affiliation_source_url"] = affiliation.source_url
        analysis["affiliation_evidence"] = affiliation.evidence.strip()
        analysis["affiliation_method"] = "manual"
        analysis["affiliation_reviewed"] = True
        connection.execute(
            "UPDATE papers SET institution = ?, country = ?, analysis = ? WHERE arxiv_id = ?",
            (
                institution,
                country,
                json.dumps(analysis, ensure_ascii=False),
                arxiv_id,
            ),
        )
    invalidate_cache()
    return {
        "arxiv_id": arxiv_id,
        "institution": institution,
        "country": country,
        "source_url": affiliation.source_url,
        "evidence": evidence,
    }


@app.post("/api/admin/papers/{arxiv_id}/summarize")
async def summarize_paper(
    arxiv_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    with connect_db() as connection:
        row = connection.execute(
            "SELECT arxiv_id, title, abstract, analysis FROM papers WHERE arxiv_id = ?",
            (arxiv_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Paper not found.")
    generated = await asyncio.to_thread(
        generate_research_analysis,
        {"title": row["title"], "abstract": row["abstract"]},
    )
    analysis = json.loads(row["analysis"])
    analysis.update(generated)
    analysis["summary_generated_at"] = datetime.now(UTC).isoformat()
    with connect_db() as connection:
        connection.execute(
            "UPDATE papers SET analysis = ? WHERE arxiv_id = ?",
            (json.dumps(analysis, ensure_ascii=False), arxiv_id),
        )
    return {"arxiv_id": arxiv_id, **generated, "summary_generated_at": analysis["summary_generated_at"]}


@app.post("/api/admin/papers/{arxiv_id}/extract-affiliation")
async def extract_documented_affiliation(
    arxiv_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    with connect_db() as connection:
        row = connection.execute(
            "SELECT title FROM papers WHERE arxiv_id = ?",
            (arxiv_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Paper not found.")
    return await asyncio.to_thread(extract_pdf_affiliations, arxiv_id, row["title"])


@app.post("/api/admin/analyze")
async def analyze_papers(
    limit: int = Query(default=20, ge=2, le=100),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    embedding_model = os.environ.get("AI_EMBEDDING_MODEL", "")
    if not embedding_model:
        raise HTTPException(status_code=503, detail="Configure AI_EMBEDDING_MODEL to enable embeddings.")
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT arxiv_id, title, abstract, analysis FROM papers
            WHERE analysis NOT LIKE ?
            ORDER BY published_at DESC, arxiv_id DESC LIMIT ?
            """,
            (f'%"embedding_model": "{embedding_model}"%', limit),
        ).fetchall()
    unanalyzed = [
        row for row in rows
        if json.loads(row["analysis"]).get("embedding_model") != embedding_model
    ]
    if len(unanalyzed) < 2:
        raise HTTPException(status_code=409, detail="At least two papers without embeddings are required.")
    texts = [f"{row['title']}\n\n{row['abstract']}" for row in unanalyzed]
    embeddings = await asyncio.to_thread(generate_embeddings, texts)
    with connect_db() as connection:
        for row, embedding in zip(unanalyzed, embeddings, strict=True):
            analysis = json.loads(row["analysis"])
            analysis["embedding"] = embedding
            analysis["embedding_model"] = embedding_model
            connection.execute(
                "UPDATE papers SET analysis = ? WHERE arxiv_id = ?",
                (json.dumps(analysis, ensure_ascii=False), row["arxiv_id"]),
            )
        embedded_rows = connection.execute(
            "SELECT arxiv_id, analysis FROM papers"
        ).fetchall()

    vectors = []
    paper_ids = []
    dimension = len(embeddings[0])
    for row in embedded_rows:
        analysis = json.loads(row["analysis"])
        embedding = analysis.get("embedding")
        if (
            analysis.get("embedding_model") == embedding_model
            and isinstance(embedding, list)
            and len(embedding) == dimension
        ):
            vectors.append(embedding)
            paper_ids.append(row["arxiv_id"])
    cluster_count = min(12, max(2, round(len(vectors) ** 0.5))) if len(vectors) > 1 else 1
    if len(vectors) > 1:
        from sklearn.cluster import KMeans

        labels = KMeans(n_clusters=cluster_count, random_state=42, n_init=10).fit_predict(vectors)
    else:
        labels = [0]
    with connect_db() as connection:
        for arxiv_id, label in zip(paper_ids, labels, strict=True):
            row = connection.execute("SELECT analysis FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
            analysis = json.loads(row["analysis"])
            analysis["topic_cluster"] = f"cluster-{int(label) + 1}"
            connection.execute(
                "UPDATE papers SET analysis = ? WHERE arxiv_id = ?",
                (json.dumps(analysis, ensure_ascii=False), arxiv_id),
            )
    return {
        "analyzed": len(unanalyzed),
        "embedded_papers": len(paper_ids),
        "clusters": cluster_count,
        "method": "OpenAI-compatible embeddings with deterministic K-Means clustering",
    }


@app.post("/api/ingest")
async def ingest_sample(
    limit: int = Query(default=100, ge=1, le=1000),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    papers, _token, _total_results = await asyncio.to_thread(fetch_oai_page, None)
    papers = papers[:limit]
    inserted = await asyncio.to_thread(insert_papers, papers)
    return {"ingested": inserted, "count": len(papers), "papers": papers}


@app.post("/api/ingest/full")
async def start_full_ingest(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    global _ingest_task
    require_admin(authorization)
    state = read_ingest_state()
    if _ingest_task and not _ingest_task.done():
        if state["status"] != "paused":
            return state
        await asyncio.gather(_ingest_task, return_exceptions=True)
        state = read_ingest_state()
    if state["status"] == "complete":
        return state
    state["status"] = "running"
    state["error"] = None
    write_ingest_state(state)
    _ingest_task = asyncio.create_task(run_full_ingest())
    return read_ingest_state()


@app.post("/api/ingest/pause")
async def pause_full_ingest(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    state = read_ingest_state()
    if state["status"] == "running":
        state["status"] = "paused"
        write_ingest_state(state)
    return state


@app.get("/api/ingest/progress")
def ingest_progress() -> dict[str, Any]:
    return read_ingest_state()


init_db()


# In production the API also serves the built website (pnpm build -> dist/), so the
# whole app runs as one service on one URL. Any non-API path falls back to index.html.
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).resolve().parent.parent / "dist"))
if (STATIC_DIR / "index.html").is_file():
    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found.")
        candidate = (STATIC_DIR / full_path).resolve()
        if full_path and candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
