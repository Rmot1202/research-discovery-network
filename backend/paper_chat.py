"""Research assistant that answers questions from the imported paper archive.

The assistant uses retrieval-augmented generation (RAG): every question is
matched against the full archive with a full-text index, the most relevant
papers are handed to the configured chat model, and the model is instructed to
answer only from those papers and cite them by arXiv ID. This lets the model
"know" every imported paper without sending the whole archive to the provider.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.ai_service import ai_request, generate_embeddings
from backend.taxonomy import CATEGORY_CODES, CATEGORY_LABELS

STOP_WORDS = {
    "a", "about", "above", "after", "all", "also", "an", "and", "any", "are", "as", "at", "be",
    "been", "being", "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing",
    "each", "few", "find", "for", "from", "give", "has", "have", "how", "i", "if", "in", "into", "is",
    "it", "its", "list", "me", "more", "most", "my", "no", "not", "of", "on", "or", "other", "our",
    "out", "over", "paper", "papers", "please", "recent", "research", "show", "so", "some", "such",
    "summarize", "tell", "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "to", "under", "up", "use", "used", "using", "very", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "why", "will", "with", "would", "you", "your",
    "work", "works", "study", "studies", "explain", "anything", "any", "know", "about",
}

_index_lock = threading.Lock()
_last_index_sync = 0.0
INDEX_SYNC_INTERVAL = 60.0


def ai_chat_configured() -> bool:
    return bool(os.environ.get("AI_BASE_URL") and os.environ.get("AI_CHAT_MODEL"))


def extract_keywords(question: str, limit: int = 12) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#-]{1,}", question.lower())
    keywords: list[str] = []
    for word in words:
        word = word.strip("-")
        if len(word) < 2 or word in STOP_WORDS or word in keywords:
            continue
        keywords.append(word)
    return keywords[:limit]


def ensure_search_index(engine: Engine, *, force: bool = False) -> None:
    """Create (once) and incrementally sync the full-text index over every paper."""
    global _last_index_sync
    now = time.monotonic()
    if not force and now - _last_index_sync < INDEX_SYNC_INTERVAL:
        return
    with _index_lock:
        if not force and time.monotonic() - _last_index_sync < INDEX_SYNC_INTERVAL:
            return
        with engine.begin() as connection:
            if engine.dialect.name == "sqlite":
                connection.execute(text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5("
                    "arxiv_id UNINDEXED, title, abstract, authors, tokenize='porter unicode61')"
                ))
                connection.execute(text(
                    "INSERT INTO papers_fts (arxiv_id, title, abstract, authors) "
                    "SELECT arxiv_id, title, abstract, authors FROM papers "
                    "WHERE arxiv_id NOT IN (SELECT arxiv_id FROM papers_fts)"
                ))
            elif engine.dialect.name == "postgresql":
                connection.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_papers_fulltext ON papers USING GIN "
                    "(to_tsvector('english', coalesce(title, '') || ' ' || coalesce(abstract, '')))"
                ))
        _last_index_sync = time.monotonic()


def _keyword_candidates(
    engine: Engine,
    keywords: list[str],
    category: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not keywords:
        return []
    params: dict[str, Any] = {"limit": limit}
    category_clause = ""
    if category:
        category_clause = (
            " AND EXISTS (SELECT 1 FROM paper_categories pc "
            "WHERE pc.arxiv_id = p.arxiv_id AND pc.category = :category)"
        )
        params["category"] = category
    if engine.dialect.name == "sqlite":
        params["match"] = " OR ".join(f'"{keyword.replace(chr(34), "")}"' for keyword in keywords)
        sql = (
            "SELECT p.arxiv_id, p.title, p.abstract, p.authors, p.published_at, p.primary_category, "
            "p.raw_metadata, p.analysis, -bm25(papers_fts, 0.0, 6.0, 1.0, 0.5) AS relevance "
            "FROM papers_fts JOIN papers p ON p.arxiv_id = papers_fts.arxiv_id "
            f"WHERE papers_fts MATCH :match{category_clause} "
            "ORDER BY relevance DESC LIMIT :limit"
        )
    elif engine.dialect.name == "postgresql":
        params["tsquery"] = " | ".join(re.sub(r"[^a-z0-9]", "", keyword) for keyword in keywords if re.sub(r"[^a-z0-9]", "", keyword))
        if not params["tsquery"]:
            return []
        document = "to_tsvector('english', coalesce(p.title, '') || ' ' || coalesce(p.abstract, ''))"
        sql = (
            "SELECT p.arxiv_id, p.title, p.abstract, p.authors, p.published_at, p.primary_category, "
            "p.raw_metadata, p.analysis, "
            f"ts_rank_cd({document}, to_tsquery('english', :tsquery)) AS relevance "
            f"FROM papers p WHERE {document} @@ to_tsquery('english', :tsquery){category_clause} "
            "ORDER BY relevance DESC LIMIT :limit"
        )
    else:
        like_clauses = []
        for index, keyword in enumerate(keywords):
            params[f"k{index}"] = f"%{keyword}%"
            like_clauses.append(f"LOWER(p.title) LIKE :k{index} OR LOWER(p.abstract) LIKE :k{index}")
        sql = (
            "SELECT p.arxiv_id, p.title, p.abstract, p.authors, p.published_at, p.primary_category, "
            "p.raw_metadata, p.analysis, 1.0 AS relevance FROM papers p "
            f"WHERE ({' OR '.join(like_clauses)}){category_clause} LIMIT :limit"
        )
    with engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def retrieve_papers(
    engine: Engine,
    question: str,
    *,
    category: str | None = None,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], str]:
    """Return the papers most relevant to the question and the retrieval method used."""
    if category and category not in CATEGORY_CODES:
        raise HTTPException(status_code=422, detail="Unknown computer science category.")
    ensure_search_index(engine)
    keywords = extract_keywords(question)
    candidates = _keyword_candidates(engine, keywords, category, limit=40)
    method = "full-text"

    # Optional semantic re-ranking with stored embeddings (from the admin "analyze" step).
    embedding_model = os.environ.get("AI_EMBEDDING_MODEL", "")
    if candidates and embedding_model and os.environ.get("AI_BASE_URL"):
        embedded = []
        for candidate in candidates:
            try:
                analysis = json.loads(candidate["analysis"] or "{}")
            except json.JSONDecodeError:
                analysis = {}
            if analysis.get("embedding_model") == embedding_model and isinstance(analysis.get("embedding"), list):
                embedded.append((candidate, analysis["embedding"]))
        if embedded:
            try:
                query_vector = generate_embeddings([question])[0]
                top_relevance = max(float(c["relevance"] or 0) for c in candidates) or 1.0
                vectors = {c["arxiv_id"]: v for c, v in embedded}
                for candidate in candidates:
                    lexical = float(candidate["relevance"] or 0) / top_relevance
                    semantic = _cosine(query_vector, vectors[candidate["arxiv_id"]]) if candidate["arxiv_id"] in vectors else 0.0
                    candidate["relevance"] = 0.5 * lexical + 0.5 * semantic
                candidates.sort(key=lambda c: c["relevance"], reverse=True)
                method = "full-text + embeddings"
            except HTTPException:
                pass

    papers = []
    for candidate in candidates[:limit]:
        try:
            raw = json.loads(candidate["raw_metadata"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        try:
            analysis = json.loads(candidate["analysis"] or "{}")
        except json.JSONDecodeError:
            analysis = {}
        authors = raw.get("authors")
        if not isinstance(authors, list):
            try:
                authors = json.loads(candidate["authors"] or "[]")
            except json.JSONDecodeError:
                authors = []
        papers.append({
            "arxiv_id": candidate["arxiv_id"],
            "title": candidate["title"],
            "abstract": candidate["abstract"],
            "authors": authors,
            "published_at": candidate["published_at"],
            "primary_category": candidate["primary_category"],
            "topic": CATEGORY_LABELS.get(candidate["primary_category"], candidate["primary_category"]),
            "url": raw.get("url") or f"https://arxiv.org/abs/{candidate['arxiv_id']}",
            "summary": analysis.get("summary"),
            "relevance": round(float(candidate["relevance"] or 0), 4),
        })
    return papers, method


def collection_stats(connect_db: Callable[[], Any]) -> dict[str, Any]:
    with connect_db() as connection:
        total = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        counts = {
            row["category"]: row["count"]
            for row in connection.execute(
                "SELECT category, COUNT(*) AS count FROM paper_categories GROUP BY category"
            ).fetchall()
        }
        years = connection.execute(
            "SELECT MIN(substr(published_at, 1, 4)) AS first_year, MAX(substr(published_at, 1, 4)) AS last_year "
            "FROM papers WHERE length(published_at) >= 4"
        ).fetchone()
    return {
        "total_papers": total,
        "first_year": years["first_year"],
        "last_year": years["last_year"],
        "papers_per_topic": {
            f"{code} {CATEGORY_LABELS[code]}": counts.get(code, 0)
            for code in sorted(CATEGORY_CODES, key=lambda c: -counts.get(c, 0))
        },
    }


SYSTEM_PROMPT = (
    "You are the research assistant for the Research Discovery Network, an archive of arXiv "
    "Computer Science papers. Answer using ONLY the collection statistics and the retrieved papers "
    "supplied in the user message. Cite every paper you mention inline as [arXiv:ID]. If the "
    "retrieved papers do not answer the question, say so plainly and suggest a better search term "
    "or topic; never invent papers, results, authors, or affiliations. Do not infer author identity, "
    "demographics, institution, or country from names. When asked whether research is "
    "under-researched or underrepresented, use the research_gap_scores in the collection data and "
    "explain which signals drive the score. Keep answers concise: a short direct answer, "
    "then a bulleted list of the most relevant papers with one sentence each on why they matter."
)


def answer_question(
    question: str,
    history: list[dict[str, str]],
    papers: list[dict[str, Any]],
    stats: dict[str, Any],
    category: str | None,
) -> str:
    model = os.environ.get("AI_CHAT_MODEL", "")
    if not model:
        raise HTTPException(status_code=503, detail="Configure AI_CHAT_MODEL to enable the research assistant.")
    context = {
        "collection": stats,
        "topic_scope": f"{category} {CATEGORY_LABELS.get(category, '')}" if category else "all topics",
        "retrieved_papers": [
            {
                "arxiv_id": paper["arxiv_id"],
                "title": paper["title"],
                "authors": paper["authors"][:8],
                "year": (paper["published_at"] or "")[:4],
                "topic": paper["topic"],
                "abstract": (paper["abstract"] or "")[:1800],
                **({"reviewed_summary": paper["summary"]} if paper.get("summary") else {}),
            }
            for paper in papers
        ],
    }
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history[-8:]:
        if turn.get("role") in {"user", "assistant"} and isinstance(turn.get("content"), str):
            messages.append({"role": turn["role"], "content": turn["content"][:4000]})
    messages.append({
        "role": "user",
        "content": f"Question: {question}\n\nArchive context (JSON):\n{json.dumps(context, ensure_ascii=False)}",
    })
    result = ai_request("chat/completions", {"model": model, "temperature": 0.2, "messages": messages})
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise HTTPException(status_code=502, detail="AI provider returned an invalid chat response.") from error
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=502, detail="AI provider returned an empty answer.")
    return content.strip()
