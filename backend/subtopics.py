"""Deeper levels of the topic map: subtopics inside one arXiv category, and their papers.

When the analysis pipeline has run, subtopics come from embedding clusters stored in the
`subtopics` / `paper_subtopics` tables. Before that, a quick keyword grouping of the
category's most recent papers is computed on the fly (and cached), so the drill-down works
on a fresh install too.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from backend.taxonomy import CATEGORY_CODES, CATEGORY_LABELS

FALLBACK_SAMPLE = 1500
CACHE_SECONDS = 1800
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _paper_card(row: Any) -> dict[str, Any]:
    try:
        analysis = json.loads(row["analysis"] or "{}")
    except (TypeError, json.JSONDecodeError):
        analysis = {}
    try:
        authors = json.loads(row["authors"] or "[]")
    except (TypeError, json.JSONDecodeError):
        authors = []
    abstract = row["abstract"] or ""
    return {
        "arxiv_id": row["arxiv_id"],
        "title": row["title"],
        "authors": authors[:6],
        "author_count": len(authors),
        "published_at": row["published_at"],
        "institution": row["institution"],
        "country": row["country"],
        "url": f"https://arxiv.org/abs/{row['arxiv_id']}",
        "summary": analysis.get("summary"),
        "key_contribution": analysis.get("key_contribution"),
        "relevance": analysis.get("relevance"),
        "abstract": abstract[:600] + ("…" if len(abstract) > 600 else ""),
    }


def _approval_clause(approval: str | None) -> tuple[str, list[Any]]:
    if approval == "public":
        return " AND p.approval_status <> ?", ["rejected"]
    if approval:
        return " AND p.approval_status = ?", [approval]
    return "", []


def _stored(connect_db: Callable[[], Any], category: str, per_cluster: int, approval: str | None) -> dict[str, Any] | None:
    clause, values = _approval_clause(approval)
    with connect_db() as connection:
        clusters = connection.execute(
            "SELECT cluster_id, label, terms, size FROM subtopics WHERE category = ? ORDER BY size DESC, cluster_id",
            (category,),
        ).fetchall()
        if not clusters:
            return None
        result = []
        for cluster in clusters:
            rows = connection.execute(
                "SELECT p.arxiv_id, p.title, p.abstract, p.authors, p.published_at, p.institution, p.country, p.analysis "
                "FROM paper_subtopics s JOIN papers p ON p.arxiv_id = s.arxiv_id "
                f"WHERE s.category = ? AND s.cluster_id = ?{clause} "
                "ORDER BY s.distance ASC, p.published_at DESC LIMIT ?",
                (category, cluster["cluster_id"], *values, per_cluster),
            ).fetchall()
            result.append({
                "id": f"{category}:{cluster['cluster_id']}",
                "label": cluster["label"],
                "terms": json.loads(cluster["terms"]),
                "size": cluster["size"],
                "papers": [_paper_card(row) for row in rows],
            })
    return {"source": "embeddings", "clusters": result}


def _keyword_fallback(connect_db: Callable[[], Any], category: str, per_cluster: int, approval: str | None) -> dict[str, Any]:
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

    from backend.analysis_pipeline import ACADEMIC_STOP_WORDS, label_clusters, readable_label, subtopic_count

    clause, values = _approval_clause(approval)
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT p.arxiv_id, p.title, p.abstract, p.authors, p.published_at, p.institution, p.country, p.analysis "
            f"FROM papers p WHERE p.primary_category = ?{clause} ORDER BY p.published_at DESC LIMIT ?",
            (category, *values, FALLBACK_SAMPLE),
        ).fetchall()
    if not rows:
        return {"source": "keywords", "clusters": []}
    documents = [f"{row['title']} {row['title']} {(row['abstract'] or '')[:700]}" for row in rows]
    cluster_count = min(subtopic_count(len(rows)), 8)
    if cluster_count == 1:
        labels = np.zeros(len(rows), dtype=int)
        closeness = np.zeros(len(rows))
    else:
        vectorizer = TfidfVectorizer(stop_words=sorted(ENGLISH_STOP_WORDS | ACADEMIC_STOP_WORDS), max_features=20000,
                                     min_df=2, sublinear_tf=True, token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9\-]{2,}\b")
        try:
            matrix = vectorizer.fit_transform(documents)
            model = KMeans(n_clusters=cluster_count, random_state=42, n_init=4)
            labels = model.fit_predict(matrix)
            closeness = -np.asarray((matrix @ model.cluster_centers_.T)[np.arange(len(rows)), labels]).ravel()
        except ValueError:
            labels = np.zeros(len(rows), dtype=int)
            closeness = np.zeros(len(rows))
            cluster_count = 1
    terms = label_clusters(documents, labels, cluster_count)
    clusters = []
    for cluster in range(cluster_count):
        members = np.flatnonzero(labels == cluster)
        if not len(members):
            continue
        ordered = members[np.argsort(closeness[members], kind="stable")]
        clusters.append({
            "id": f"{category}:k{cluster}",
            "label": readable_label(terms[cluster]),
            "terms": terms[cluster],
            "size": int(len(members)),
            "papers": [_paper_card(rows[index]) for index in ordered[:per_cluster]],
        })
    clusters.sort(key=lambda item: -item["size"])
    return {"source": "keywords", "clusters": clusters, "sampled_papers": len(rows)}


def topic_drilldown(
    connect_db: Callable[[], Any],
    category: str,
    per_cluster: int = 8,
    approval: str | None = None,
) -> dict[str, Any]:
    if category not in CATEGORY_CODES:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Unknown computer science category.")
    key = (category, f"{per_cluster}:{approval}")
    with _lock:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
    try:
        body = _stored(connect_db, category, per_cluster, approval)
    except Exception:  # subtopic tables not created yet
        body = None
    if body is None:
        body = _keyword_fallback(connect_db, category, per_cluster, approval)
    with connect_db() as connection:
        total = connection.execute("SELECT COUNT(*) FROM papers WHERE primary_category = ?", (category,)).fetchone()[0]
    body = {"code": category, "label": CATEGORY_LABELS.get(category, category), "total_papers": total, **body}
    with _lock:
        _cache[key] = (time.time(), body)
    return body
