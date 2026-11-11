from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from backend.arxiv_utils import abs_url, is_openalex_id, pdf_url
from backend.discovery_score import DiscoveryIndex, ranked_ids, score_paper
from backend.taxonomy import CATEGORIES, CATEGORY_CODES, CATEGORY_LABELS


def read_paper_rows(
    connect_db: Callable[[], Any],
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
    index: DiscoveryIndex | None = None,
    weights: dict[str, float] | None = None,
) -> tuple[list[Any], int]:
    clauses: list[str] = []
    values: list[Any] = []
    if category:
        if category not in CATEGORY_CODES:
            raise HTTPException(status_code=422, detail="Unknown computer science category.")
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_categories pc "
            "WHERE pc.arxiv_id = papers.arxiv_id AND pc.category = ?)"
        )
        values.append(category)
    if country:
        clauses.append("country = ?")
        values.append(country)
    if institution:
        clauses.append("institution = ?")
        values.append(institution)
    if year is not None:
        clauses.append("published_at LIKE ?")
        values.append(f"{year}%")
    if approval_status == "public":  # PUBLISH_MODE=auto: everything except rejected papers
        clauses.append("approval_status <> ?")
        values.append("rejected")
    elif approval_status:
        clauses.append("approval_status = ?")
        values.append(approval_status)
    if query:
        search = f"%{query.strip()}%"
        matched_categories = [
            code
            for code, label in CATEGORIES
            if query.strip().lower() in code.lower() or query.strip().lower() in label.lower()
        ]
        category_search = (
            " OR EXISTS (SELECT 1 FROM paper_categories search_category "
            "WHERE search_category.arxiv_id = papers.arxiv_id AND search_category.category IN ("
            + ",".join("?" for _ in matched_categories)
            + "))"
            if matched_categories
            else ""
        )
        clauses.append(
            "(LOWER(title) LIKE LOWER(?) OR LOWER(abstract) LIKE LOWER(?) OR LOWER(authors) LIKE LOWER(?) "
            "OR LOWER(COALESCE(institution, '')) LIKE LOWER(?) "
            f"OR LOWER(COALESCE(country, '')) LIKE LOWER(?) OR LOWER(primary_category) LIKE LOWER(?){category_search})"
        )
        values.extend([search] * 6)
        values.extend(matched_categories)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    ordering = {
        "newest": "published_at DESC, arxiv_id DESC",
        "oldest": "published_at ASC, arxiv_id ASC",
        "title": "LOWER(title) ASC",
        # PUBLISH_MODE=auto featured list: editor-approved papers first, then the newest.
        "featured": "CASE WHEN approval_status = 'approved' THEN 0 ELSE 1 END, published_at DESC, arxiv_id DESC",
        "score": "published_at DESC, arxiv_id DESC",  # re-ranked by Discovery Score below
    }.get(sort)
    if ordering is None:
        raise HTTPException(status_code=422, detail="sort must be newest, oldest, title, or score.")

    with connect_db() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM papers{where}", values).fetchone()[0]
        if sort == "score" and index is not None:
            matching = [
                row[0]
                for row in connection.execute(f"SELECT arxiv_id FROM papers{where}", values).fetchall()
            ]
            page = [arxiv_id for arxiv_id, _ in ranked_ids(index, matching, weights)[offset : offset + limit]]
            if not page:
                return [], total
            by_id = {
                row["arxiv_id"]: row
                for row in connection.execute(
                    f"SELECT * FROM papers WHERE arxiv_id IN ({','.join('?' for _ in page)})", page
                ).fetchall()
            }
            return [by_id[arxiv_id] for arxiv_id in page if arxiv_id in by_id], total
        rows = connection.execute(
            f"SELECT * FROM papers{where} ORDER BY {ordering} LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
    return rows, total


def serialize_paper(
    row: Any,
    index: DiscoveryIndex,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    raw = json.loads(row["raw_metadata"])
    analysis = json.loads(row["analysis"])
    for old_key in ("representation_score", "score_components"):
        analysis.pop(old_key, None)
    discovery = score_paper(
        index,
        arxiv_id=row["arxiv_id"],
        field=row["primary_category"],
        institution=row["institution"],
        country=row["country"],
        weights=weights,
    )
    analysis["discovery_score"] = discovery["discovery_score"]
    analysis["discovery"] = discovery
    analysis["approval_status"] = row["approval_status"]
    source = row["source"] if "source" in row._mapping else None
    is_openalex = source == "openalex" or is_openalex_id(row["arxiv_id"])
    return {
        **raw,
        **analysis,
        "source": "openalex" if is_openalex else "arxiv",
        "ingested_at": row["ingested_at"] if "ingested_at" in row._mapping else None,
        "url": (raw.get("url") if is_openalex and raw.get("url") else
                abs_url(row["arxiv_id"], raw.get("categories"), row["primary_category"])),
        "pdf_url": (raw.get("pdf_url") or raw.get("url") or abs_url(row["arxiv_id"])) if is_openalex else
        pdf_url(row["arxiv_id"], raw.get("categories"), row["primary_category"]),
        "institution": row["institution"],
        "country": row["country"],
        "topic": CATEGORY_LABELS.get(row["primary_category"], row["primary_category"]),
        "primary_category": row["primary_category"],
    }


def get_filter_options(connect_db: Callable[[], Any]) -> dict[str, Any]:
    with connect_db() as connection:
        institutions = [
            row[0]
            for row in connection.execute(
                "SELECT MIN(institution) FROM papers WHERE institution IS NOT NULL "
                "GROUP BY LOWER(TRIM(institution)) ORDER BY LOWER(TRIM(institution))"
            )
        ]
        countries = [
            row[0]
            for row in connection.execute(
                "SELECT MIN(country) FROM papers WHERE country IS NOT NULL "
                "GROUP BY LOWER(TRIM(country)) ORDER BY LOWER(TRIM(country))"
            )
        ]
        years = [
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT substr(published_at, 1, 4) FROM papers "
                "WHERE length(published_at) >= 4 ORDER BY 1 DESC"
            )
            if row[0].isdigit()
        ]
        counts = {
            row["category"]: row["count"]
            for row in connection.execute(
                "SELECT category, COUNT(*) AS count FROM paper_categories GROUP BY category"
            )
        }
    return {"institutions": institutions, "countries": countries, "years": years, "counts": counts}


