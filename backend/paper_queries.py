from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

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
) -> tuple[list[Any], int, dict[str, int], dict[str, int], dict[str, int]]:
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
    if approval_status:
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
        "score": "(SELECT COUNT(*) FROM paper_categories AS topic_count "
        "WHERE topic_count.category = papers.primary_category) ASC, "
        "published_at DESC",
    }.get(sort)
    if ordering is None:
        raise HTTPException(status_code=422, detail="sort must be newest, oldest, title, or score.")

    with connect_db() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM papers{where}", values).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM papers{where} ORDER BY {ordering} LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
        category_counts = {
            row["category"]: row["count"]
            for row in connection.execute(
                "SELECT category, COUNT(*) AS count FROM paper_categories GROUP BY category"
            ).fetchall()
        }
        institution_counts = {
            row["normalized"]: row["count"]
            for row in connection.execute(
                "SELECT LOWER(TRIM(institution)) AS normalized, COUNT(*) AS count FROM papers "
                "WHERE institution IS NOT NULL GROUP BY LOWER(TRIM(institution))"
            ).fetchall()
        }
        country_counts = {
            row["normalized"]: row["count"]
            for row in connection.execute(
                "SELECT LOWER(TRIM(country)) AS normalized, COUNT(*) AS count FROM papers "
                "WHERE country IS NOT NULL GROUP BY LOWER(TRIM(country))"
            ).fetchall()
        }
    return rows, total, category_counts, institution_counts, country_counts


def serialize_paper(
    row: Any,
    category_counts: dict[str, int],
    institution_counts: dict[str, int],
    country_counts: dict[str, int],
    total: int,
) -> dict[str, Any]:
    raw = json.loads(row["raw_metadata"])
    analysis = json.loads(row["analysis"])
    category_count = category_counts.get(row["primary_category"], 0)
    total_category_assignments = sum(category_counts.values())
    rarity = (
        round(100 * (1 - category_count / total_category_assignments), 1)
        if total_category_assignments
        else None
    )
    institution_visibility = (
        round(100 * (1 - institution_counts[row["institution"].strip().lower()] / total), 1)
        if row["institution"] and total
        else None
    )
    geographic_visibility = (
        round(100 * (1 - country_counts[row["country"].strip().lower()] / total), 1)
        if row["country"] and total
        else None
    )
    available_scores = [(rarity, 0.6)]
    if institution_visibility is not None:
        available_scores.append((institution_visibility, 0.25))
    if geographic_visibility is not None:
        available_scores.append((geographic_visibility, 0.15))
    total_weight = sum(weight for _score, weight in available_scores)
    analysis["representation_score"] = round(
        sum(score * weight for score, weight in available_scores if score is not None) / total_weight,
        1,
    )
    analysis["score_components"] = {
        "topic_rarity": rarity,
        "institution_visibility": institution_visibility,
        "geographic_visibility": geographic_visibility,
        "weights": {"topic": 0.6, "institution": 0.25, "geography": 0.15},
    }
    analysis["approval_status"] = row["approval_status"]
    return {
        **raw,
        **analysis,
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


