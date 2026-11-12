"""Research gap scores: explainable estimates of whether research is under-researched
(too little work) and underrepresented (work in the collection comes from few places or groups).

Every score is computed only from the imported arXiv metadata (dates, categories,
titles, and author name strings used as opaque labels). Each component is on a
0-100 scale where HIGHER means MORE underdeveloped, and the index is their
weighted average. The same formulas are used for arXiv topics and for free-text
research ideas, so the two can be compared directly.

Components
----------
scarcity          100 x (1 - log(1 + papers) / log(1 + archive papers))
                  Few papers relative to the whole archive.
momentum_gap      50 - 50 x log2(growth), clipped to 0-100, where
                  growth = (papers in last 24 months + 1) / (papers in prior 24 months + 1).
                  Flat or shrinking output scores high; doubling scores 0.
synthesis_gap     100 / (1 + surveys / expected surveys), where expected surveys =
                  papers x archive-wide survey rate. No surveys/reviews = 100,
                  average survey coverage = 50.
isolation         100 x (1 - share of papers cross-listed in 2+ CS categories).
                  Work that rarely connects to other fields.
concentration     100 x share of papers that include one of the 5 most frequent
                  author names in the set. A field carried by a few groups.

Underrepresentation (separate score, documented affiliations only)
------------------------------------------------------------------
country_concentration      100 x (1 - normalised Shannon entropy of documented countries)
institution_concentration  100 x (1 - normalised Shannon entropy of documented institutions)
underrepresentation index  average of the two. It is reported only when at least
                           MIN_DOCUMENTED papers have documented affiliation evidence (admin-entered or found by the paper scan);
                           names are never used to guess affiliation or identity.

These are signals for prioritising human review, not measures of research
quality, importance, or the people doing it.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.arxiv_utils import abs_url
from backend.paper_chat import ensure_search_index, extract_keywords
from backend.taxonomy import CATEGORIES, CATEGORY_CODES, CATEGORY_LABELS

HMI_EXPANSIONS = (
    ("human", "machine", "interface"),
    ("human", "machine", "interaction"),
)

WEIGHTS = {
    "scarcity": 0.30,
    "momentum_gap": 0.20,
    "synthesis_gap": 0.20,
    "isolation": 0.15,
    "concentration": 0.15,
}
COMPONENT_LABELS = {
    "scarcity": "Few papers",
    "momentum_gap": "Slow growth",
    "synthesis_gap": "No surveys / reviews",
    "isolation": "Little cross-field work",
    "concentration": "Few research groups",
}
SURVEY_PATTERN = re.compile(
    r"\b(survey|review|overview|tutorial|systematic literature|state of the art|state-of-the-art|taxonomy|roadmap)\b",
    re.IGNORECASE,
)
CACHE_SECONDS = 600
MIN_DOCUMENTED = 10

_cache_lock = threading.Lock()
_build_lock = threading.Lock()
_cache: dict[str, Any] = {"built": 0.0, "count": -1, "corpus": None}


def _shift_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, month = divmod(month_index, 12)
    return date(year, month + 1, min(day.day, 28))


def invalidate_cache() -> None:
    """Called after affiliation updates so the next report re-reads the archive."""
    with _cache_lock:
        _cache["count"] = -1
    from backend import discovery_score

    discovery_score.invalidate()


def _cached_corpus(count: int) -> dict[str, Any] | None:
    if _cache["corpus"] is not None and _cache["count"] == count and time.monotonic() - _cache["built"] < CACHE_SECONDS:
        return _cache["corpus"]
    return None


# Only the analysis JSON of papers with scanned affiliations is needed; the rest (summaries, older
# inline embeddings) stays in the database. Rows are streamed in batches, so peak memory stays low
# on small hosts (Render free: 512 MB).
CORPUS_QUERY = (
    "SELECT arxiv_id, published_at, title, authors, institution, country, "
    "CASE WHEN analysis LIKE '%institutions_all%' THEN analysis ELSE NULL END AS analysis FROM papers"
)
CORPUS_BATCH = 2000


def _load_corpus(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM papers")).scalar_one()
    cached = _cached_corpus(count)
    if cached is not None:
        return cached
    with _build_lock:  # one build at a time; requests that waited reuse the fresh result
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM papers")).scalar_one()
        cached = _cached_corpus(count)
        if cached is not None:
            return cached
        try:
            return _build_corpus(engine, count)
        except Exception:  # a pooler that refuses server-side cursors: read normally instead
            return _build_corpus(engine, count, stream=False)


def _build_corpus(engine: Engine, count: int, stream: bool = True) -> dict[str, Any]:
    categories: dict[str, list[str]] = {}
    papers: dict[str, dict[str, Any]] = {}
    with engine.connect() as connection:
        for arxiv_id, category in connection.execute(text("SELECT arxiv_id, category FROM paper_categories")):
            if category in CATEGORY_CODES:
                categories.setdefault(arxiv_id, []).append(category)
        ingest = connection.execute(text("SELECT state_json FROM ingest_state WHERE id = 1")).first()
        options = {"stream_results": True, "yield_per": CORPUS_BATCH} if stream else {}
        result = connection.execution_options(**options).execute(text(CORPUS_QUERY))
        for arxiv_id, published_at, title, authors, institution, country, analysis_json in result:
            papers[arxiv_id] = _paper_entry(published_at, title, authors, institution, country, analysis_json,
                                            categories.get(arxiv_id, []))
    return _finish_corpus(papers, ingest, count)


def _paper_entry(published_at: Any, title: Any, authors: Any, institution: Any, country: Any,
                 analysis_json: Any, paper_categories: list[str]) -> dict[str, Any]:
    institutions_all: list[str] = []
    countries_all: list[str] = []
    if analysis_json and "institutions_all" in analysis_json:
        try:
            analysis = json.loads(analysis_json)
            institutions_all = [" ".join(str(v).split()) for v in analysis.get("institutions_all") or [] if v]
            countries_all = [" ".join(str(v).split()) for v in analysis.get("countries_all") or [] if v]
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        author_list = json.loads(authors) if authors else []
    except (json.JSONDecodeError, TypeError):
        author_list = []
    return {
        "date": (published_at or "")[:10],
        "survey": bool(SURVEY_PATTERN.search(title or "")),
        "authors": [" ".join(str(name).lower().split()) for name in author_list if name][:30],
        "categories": paper_categories,
        "institution": " ".join(institution.split()) if institution else None,
        "country": " ".join(country.split()) if country else None,
        # Every documented institution / country on the paper (from the paper scan), else the primary one.
        "institutions": institutions_all or ([" ".join(institution.split())] if institution else []),
        "countries": countries_all or ([" ".join(country.split())] if country else []),
    }


def _finish_corpus(papers: dict[str, dict[str, Any]], ingest: Any, count: int) -> dict[str, Any]:
    dates = [paper["date"] for paper in papers.values() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", paper["date"])]
    latest = date.fromisoformat(max(dates)) if dates else date.today()
    survey_total = sum(1 for paper in papers.values() if paper["survey"])
    ingest_status = None
    if ingest:
        try:
            ingest_status = json.loads(ingest[0]).get("status")
        except (json.JSONDecodeError, TypeError):
            ingest_status = None
    corpus = {
        "papers": papers,
        "total": len(papers),
        "latest": latest,
        "recent_start": _shift_months(latest, -24).isoformat(),
        "previous_start": _shift_months(latest, -48).isoformat(),
        "survey_rate": survey_total / len(papers) if papers else 0.0,
        "ingest_status": ingest_status,
    }
    with _cache_lock:
        _cache.update({"built": time.monotonic(), "count": count, "corpus": corpus})
    return corpus


def _clip(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def _concentration(values: list[str]) -> float:
    """100 = every paper from one place, 0 = evenly spread across many."""
    counts = Counter(value.casefold() for value in values)
    if len(counts) <= 1:
        return 100.0
    total = sum(counts.values())
    entropy = -sum((n / total) * math.log(n / total) for n in counts.values())
    # Normalise against an even spread over up to 50 places, so 2-3 places still reads as concentrated.
    return _clip(100 * (1 - entropy / math.log(max(2, min(total, 50)))))


def representation_for(papers: list[dict[str, Any]]) -> dict[str, Any]:
    # Each institution/country is counted once per paper it appears on.
    countries = [name for paper in papers for name in paper.get("countries", [paper["country"]] if paper.get("country") else [])]
    institutions = [name for paper in papers for name in paper.get("institutions", [paper["institution"]] if paper.get("institution") else [])]
    documented = sum(1 for paper in papers if paper.get("country") or paper.get("institution"))
    enough = documented >= MIN_DOCUMENTED
    components = {
        "country_concentration": _concentration(countries) if len(countries) >= MIN_DOCUMENTED else None,
        "institution_concentration": _concentration(institutions) if len(institutions) >= MIN_DOCUMENTED else None,
    }
    available = [value for value in components.values() if value is not None]
    index = round(sum(available) / len(available), 1) if enough and available else None
    if index is None:
        verdict = "Not enough documented affiliations"
    elif index >= 60:
        verdict = "Underrepresented"
    elif index >= 40:
        verdict = "Somewhat concentrated"
    else:
        verdict = "Broadly represented"
    return {
        "index": index,
        "verdict": verdict,
        "components": components,
        "documented_papers": documented,
        "documented_pct": round(100 * documented / len(papers), 1) if papers else 0.0,
        "min_documented": MIN_DOCUMENTED,
        "top_countries": [{"name": name, "papers": n} for name, n in Counter(countries).most_common(4)],
        "top_institutions": [{"name": name, "papers": n} for name, n in Counter(institutions).most_common(4)],
    }


def score_paper_set(corpus: dict[str, Any], arxiv_ids: list[str]) -> dict[str, Any]:
    papers = [corpus["papers"][arxiv_id] for arxiv_id in arxiv_ids if arxiv_id in corpus["papers"]]
    count = len(papers)
    total = max(1, corpus["total"])
    recent = sum(1 for paper in papers if paper["date"] > corpus["recent_start"])
    previous = sum(1 for paper in papers if corpus["previous_start"] < paper["date"] <= corpus["recent_start"])
    surveys = sum(1 for paper in papers if paper["survey"])
    cross_listed = sum(1 for paper in papers if len(paper["categories"]) >= 2)
    author_counts = Counter(name for paper in papers for name in set(paper["authors"]))
    top_authors = {name for name, _ in author_counts.most_common(5)}
    concentrated = sum(1 for paper in papers if top_authors.intersection(paper["authors"]))
    growth = (recent + 1) / (previous + 1)
    expected_surveys = count * corpus["survey_rate"]

    components = {
        "scarcity": _clip(100 * (1 - math.log1p(count) / math.log1p(total))),
        "momentum_gap": _clip(50 - 50 * math.log2(growth)),
        "synthesis_gap": _clip(100 / (1 + surveys / expected_surveys)) if expected_surveys > 0 else 100.0,
        "isolation": _clip(100 * (1 - cross_listed / count)) if count else 100.0,
        "concentration": _clip(100 * concentrated / count) if count else 100.0,
    }
    index = round(sum(components[key] * weight for key, weight in WEIGHTS.items()), 1)
    if count < 30:
        confidence = "low"
    elif count < 300:
        confidence = "medium"
    else:
        confidence = "high"
    if count == 0:
        verdict = "No papers found"
    elif index >= 62 and growth >= 1.3:
        verdict = "Under-researched, growing"
    elif index >= 62:
        verdict = "Under-researched"
    elif index >= 45:
        verdict = "Developing"
    else:
        verdict = "Well developed"
    drivers = sorted(components, key=lambda key: components[key] * WEIGHTS[key], reverse=True)[:2]
    return {
        "index": index,
        "verdict": verdict,
        "confidence": confidence,
        "components": components,
        "drivers": [COMPONENT_LABELS[key] for key in drivers],
        "representation": representation_for(papers),
        "metrics": {
            "papers": count,
            "share_of_archive_pct": round(100 * count / total, 3),
            "recent_24_months": recent,
            "previous_24_months": previous,
            "growth_ratio": round(growth, 2),
            "surveys": surveys,
            "expected_surveys": round(expected_surveys, 1),
            "cross_listed_pct": round(100 * cross_listed / count, 1) if count else 0.0,
            "top5_author_share_pct": round(100 * concentrated / count, 1) if count else 0.0,
            "distinct_authors": len(author_counts),
        },
    }


def _methodology(corpus: dict[str, Any]) -> dict[str, Any]:
    note = None
    if corpus["ingest_status"] != "complete":
        note = (
            "The arXiv import is not complete, so counts and growth reflect the imported sample "
            "rather than all of arXiv."
        )
    return {
        "weights": WEIGHTS,
        "component_labels": COMPONENT_LABELS,
        "archive_papers": corpus["total"],
        "latest_paper_date": corpus["latest"].isoformat(),
        "recent_window_start": corpus["recent_start"],
        "previous_window_start": corpus["previous_start"],
        "archive_survey_rate_pct": round(100 * corpus["survey_rate"], 2),
        "coverage_note": note,
    }


def topic_gap_report(engine: Engine) -> dict[str, Any]:
    corpus = _load_corpus(engine)
    by_topic: dict[str, list[str]] = {code: [] for code in CATEGORY_CODES}
    for arxiv_id, paper in corpus["papers"].items():
        for category in paper["categories"]:
            by_topic[category].append(arxiv_id)
    topics = []
    for code, label in CATEGORIES:
        topics.append({"code": code, "label": label, **score_paper_set(corpus, by_topic[code])})
    # Topics with no imported papers go last: the sample simply has not reached them yet.
    topics.sort(key=lambda topic: (topic["metrics"]["papers"] > 0, topic["index"]), reverse=True)
    return {"methodology": _methodology(corpus), "topics": topics}


def _idea_keyword_variants(query: str) -> list[list[str]]:
    keywords = extract_keywords(query.replace("-", " "))
    variants = [keywords]
    if "hmi" in keywords:
        for expansion in HMI_EXPANSIONS:
            variants.append([
                expanded
                for keyword in keywords
                for expanded in (expansion if keyword == "hmi" else (keyword,))
            ])
    for start in range(len(keywords) - 2):
        phrase = keywords[start:start + 3]
        if phrase[:2] == ["human", "machine"] and phrase[2] in {"interface", "interfaces", "interaction", "interactions"}:
            context_before, context_after = keywords[:start], keywords[start + 3:]
            variants.append(context_before + ["hmi"] + context_after)
            for expansion in HMI_EXPANSIONS:
                variants.append(context_before + list(expansion) + context_after)
    return [list(variant) for variant in dict.fromkeys(tuple(variant) for variant in variants)]


def _matching_ids(engine: Engine, query: str, category: str | None, limit: int = 20000) -> list[str]:
    variants = _idea_keyword_variants(query)
    keywords = variants[0] if variants else []
    if not keywords:
        raise HTTPException(status_code=422, detail="Describe the research idea with a few specific keywords.")
    ensure_search_index(engine)
    params: dict[str, Any] = {"limit": limit}
    category_clause = ""
    if category:
        category_clause = (
            " AND EXISTS (SELECT 1 FROM paper_categories pc WHERE pc.arxiv_id = p.arxiv_id AND pc.category = :category)"
        )
        params["category"] = category
    if engine.dialect.name == "sqlite":
        # Every keyword in a variant must appear; alternatives support equivalent spellings.
        params["match"] = " OR ".join(
            "(" + " AND ".join(f'"{keyword.replace(chr(34), "")}"' for keyword in variant) + ")"
            for variant in variants
        )
        sql = (
            "SELECT p.arxiv_id FROM papers_fts JOIN papers p ON p.arxiv_id = papers_fts.arxiv_id "
            f"WHERE papers_fts MATCH :match{category_clause} LIMIT :limit"
        )
    elif engine.dialect.name == "postgresql":
        params["tsquery"] = " | ".join(
            "(" + " & ".join(term for keyword in variant
                             if (term := re.sub(r"[^a-z0-9]", "", keyword))) + ")"
            for variant in variants
        )
        if not params["tsquery"]:
            return []
        document = "to_tsvector('english', coalesce(p.title, '') || ' ' || coalesce(p.abstract, ''))"
        sql = (
            f"SELECT p.arxiv_id FROM papers p WHERE {document} @@ to_tsquery('english', :tsquery)"
            f"{category_clause} LIMIT :limit"
        )
    else:
        variant_clauses = []
        index = 0
        for variant in variants:
            clauses = []
            for keyword in variant:
                params[f"k{index}"] = f"%{keyword}%"
                clauses.append(f"(LOWER(p.title) LIKE :k{index} OR LOWER(p.abstract) LIKE :k{index})")
                index += 1
            variant_clauses.append("(" + " AND ".join(clauses) + ")")
        sql = f"SELECT p.arxiv_id FROM papers p WHERE {' OR '.join(variant_clauses)}{category_clause} LIMIT :limit"
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(text(sql), params).all()]


def idea_gap_report(engine: Engine, query: str, category: str | None = None) -> dict[str, Any]:
    if category and category not in CATEGORY_CODES:
        raise HTTPException(status_code=422, detail="Unknown computer science category.")
    corpus = _load_corpus(engine)
    ids = _matching_ids(engine, query, category)
    result = score_paper_set(corpus, ids)
    topic_counts = Counter(category for arxiv_id in ids for category in corpus["papers"].get(arxiv_id, {}).get("categories", []))
    newest = sorted(ids, key=lambda arxiv_id: corpus["papers"][arxiv_id]["date"], reverse=True)[:6]
    sample = []
    if newest:
        with engine.connect() as connection:
            for arxiv_id in newest:
                row = connection.execute(
                    text("SELECT arxiv_id, title, published_at, primary_category FROM papers WHERE arxiv_id = :id"),
                    {"id": arxiv_id},
                ).mappings().first()
                if row:
                    sample.append({
                        "arxiv_id": row["arxiv_id"],
                        "title": row["title"],
                        "year": (row["published_at"] or "")[:4],
                        "topic": CATEGORY_LABELS.get(row["primary_category"], row["primary_category"]),
                        "url": abs_url(row["arxiv_id"], None, row["primary_category"]),
                    })
    topics = topic_gap_report(engine)["topics"]
    indices = sorted(topic["index"] for topic in topics if topic["metrics"]["papers"] > 0)
    below = sum(1 for value in indices if value < result["index"])
    return {
        "query": query,
        "keywords": extract_keywords(query.replace("-", " ")),
        "category": category,
        **result,
        "topic_percentile": round(100 * below / len(indices), 1) if indices else None,
        "main_topics": [
            {"code": code, "label": CATEGORY_LABELS[code], "papers": papers}
            for code, papers in topic_counts.most_common(4)
        ],
        "sample_papers": sample,
        "methodology": _methodology(corpus),
    }


def gap_summary_for_chat(engine: Engine) -> list[str]:
    try:
        topics = topic_gap_report(engine)["topics"]
    except Exception:
        return []
    return [(
            f"{topic['code']} {topic['label']}: under-research index {topic['index']} ({topic['verdict']}); "
            f"underrepresentation {topic['representation']['index'] if topic['representation']['index'] is not None else 'n/a'} "
            f"({topic['representation']['verdict']})"
        ) for topic in topics]
