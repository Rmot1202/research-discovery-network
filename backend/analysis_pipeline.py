"""Automatic AI research analysis and summarization for every paper (URD components 3 and 5).

Everything here runs on free, local tools by default, so there is no per-paper cost:

* Embeddings: `fastembed` with BAAI/bge-small-en-v1.5 (a small ONNX model that runs on any
  CPU, downloaded once, about 130 MB). If fastembed is not installed, a built-in hashing
  embedder is used instead (no download, lower quality). An OpenAI-compatible embedding API
  is used only when you set EMBEDDING_BACKEND=api.
* Subtopics: inside every arXiv category, papers are grouped with K-Means on their
  embeddings. Each group gets a readable label from its most distinctive title/abstract
  terms (class-based TF-IDF). These groups are the "deeper" level of the 3D map.
* Summaries: plain-language summary, key contribution, application, why it matters, plus
  topics / methods / context, written by a local open model through Ollama
  (default qwen2.5:3b). Any OpenAI-compatible endpoint works too (AI_BASE_URL).

Each stage is resumable: papers that were already embedded, grouped or summarized are skipped,
so you can stop with Ctrl+C and run the same command again.

    python -m backend.analysis_pipeline all                    # everything, as far as time allows
    python -m backend.analysis_pipeline embed
    python -m backend.analysis_pipeline cluster [--recluster]
    python -m backend.analysis_pipeline summarize --limit 500 --max-minutes 60
    python -m backend.analysis_pipeline status
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy.engine import Engine

from backend.database import create_database_engine, database_connection

LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
HASHING_MODEL = "hashing-512"
DEFAULT_LOCAL_LLM_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LOCAL_LLM = "qwen2.5:3b"
EMBED_BATCH = 128
MAX_SUBTOPICS = 10
RECLUSTER_GROWTH = 0.25  # re-group a category once it has grown by 25% since its last grouping

ACADEMIC_STOP_WORDS = {
    "paper", "papers", "propose", "proposed", "proposes", "approach", "approaches", "method", "methods",
    "result", "results", "show", "shows", "shown", "based", "using", "use", "used", "uses", "new", "novel",
    "work", "study", "studies", "problem", "problems", "present", "presents", "existing", "different",
    "performance", "framework", "model", "models", "data", "task", "tasks", "et", "al", "demonstrate",
    "demonstrates", "experiments", "experimental", "state", "art", "significantly", "improve", "improves",
    "improved", "achieve", "achieves", "furthermore", "however", "via", "towards", "toward", "large", "high",
    "low", "set", "sets", "provide", "provides", "introduce", "introduces", "address", "key", "various",
    "applications", "application", "including", "effective", "efficient", "efficiently", "effectively",
    "recent", "recently", "challenge", "challenges", "challenging", "analysis", "learning", "systems", "system",
    "evaluate", "evaluation", "benchmark", "benchmarks", "code", "available", "github", "com", "https", "http",
    "compared", "achieving", "outperforms", "superior", "extensive", "leveraging", "leverages", "leverage",
}


# ----------------------------------------------------------------------------- storage

def ensure_tables(engine: Engine) -> None:
    with database_connection(engine) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS paper_embeddings ("
            "arxiv_id TEXT PRIMARY KEY, model TEXT NOT NULL, vector TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS subtopics ("
            "category TEXT NOT NULL, cluster_id INTEGER NOT NULL, label TEXT NOT NULL, terms TEXT NOT NULL, "
            "size INTEGER NOT NULL, centroid TEXT NOT NULL, model TEXT NOT NULL, clustered_size INTEGER NOT NULL, "
            "updated_at TEXT NOT NULL, PRIMARY KEY (category, cluster_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS paper_subtopics ("
            "arxiv_id TEXT PRIMARY KEY, category TEXT NOT NULL, cluster_id INTEGER NOT NULL, distance REAL NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_subtopics_cluster ON paper_subtopics(category, cluster_id, distance)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS paper_summary_runs ("
            "arxiv_id TEXT PRIMARY KEY, status TEXT NOT NULL, model TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL)"
        )


def encode_vector(vector: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vector, dtype=np.float16).tobytes()).decode("ascii")


def decode_vector(value: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(value), dtype=np.float16).astype(np.float32)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return matrix / norms


# ----------------------------------------------------------------------------- embedders

class HashingEmbedder:
    """No-download fallback: hashed word and word-pair counts projected to 512 dimensions."""

    model = HASHING_MODEL

    def __init__(self, dimension: int = 512) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, value in enumerate(texts):
            words = [word for word in re.findall(r"[a-z][a-z0-9\-]+", value.lower()) if word not in ACADEMIC_STOP_WORDS]
            for token in words + [f"{a} {b}" for a, b in zip(words, words[1:])]:
                digest = int(hashlib.blake2b(token.encode(), digest_size=8).hexdigest(), 16)
                matrix[row, digest % self.dimension] += 1.0 if (digest >> 32) & 1 else -1.0
        return _normalize(np.sign(matrix) * np.log1p(np.abs(matrix)))


class FastEmbedder:
    def __init__(self, model: str = LOCAL_EMBEDDING_MODEL) -> None:
        from fastembed import TextEmbedding

        self.model = model
        cache = os.path.expanduser(os.environ.get("FASTEMBED_CACHE_PATH", "~/.cache/fastembed"))
        self._model = TextEmbedding(model_name=model, cache_dir=cache)

    def embed(self, texts: list[str]) -> np.ndarray:
        return _normalize(np.array(list(self._model.embed(texts, batch_size=64)), dtype=np.float32))


class ApiEmbedder:
    def __init__(self) -> None:
        self.model = os.environ.get("AI_EMBEDDING_MODEL", "")
        if not self.model:
            raise RuntimeError("EMBEDDING_BACKEND=api needs AI_EMBEDDING_MODEL (and AI_BASE_URL).")

    def embed(self, texts: list[str]) -> np.ndarray:
        from backend.ai_service import generate_embeddings

        return _normalize(np.array(generate_embeddings(texts), dtype=np.float32))


def make_embedder() -> Any:
    backend = os.environ.get("EMBEDDING_BACKEND", "local").strip().lower()
    if backend == "api":
        return ApiEmbedder()
    if backend == "hashing":
        return HashingEmbedder()
    try:
        return FastEmbedder(os.environ.get("LOCAL_EMBEDDING_MODEL", LOCAL_EMBEDDING_MODEL))
    except ImportError:
        print("fastembed is not installed; using the built-in hashing embedder. "
              "Install it with: pip install -r backend/requirements-pipeline.txt", file=sys.stderr)
        return HashingEmbedder()


def paper_text(title: str, abstract: str) -> str:
    return f"{title}. {abstract[:1800]}"


# ----------------------------------------------------------------------------- stage 1: embed

def embed_papers(
    engine: Engine,
    embedder: Any | None = None,
    limit: int | None = None,
    deadline: float | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    ensure_tables(engine)
    embedder = embedder or make_embedder()
    done = 0
    with database_connection(engine) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM papers p LEFT JOIN paper_embeddings e ON e.arxiv_id = p.arxiv_id AND e.model = ? "
            "WHERE e.arxiv_id IS NULL",
            (embedder.model,),
        ).fetchone()[0]
    target = min(remaining, limit) if limit is not None else remaining
    started = time.time()
    while done < target and (deadline is None or time.time() < deadline):
        size = min(EMBED_BATCH, target - done)
        with database_connection(engine) as connection:
            rows = connection.execute(
                "SELECT p.arxiv_id, p.title, p.abstract FROM papers p "
                "LEFT JOIN paper_embeddings e ON e.arxiv_id = p.arxiv_id AND e.model = ? "
                "WHERE e.arxiv_id IS NULL ORDER BY p.published_at DESC, p.arxiv_id DESC LIMIT ?",
                (embedder.model, size),
            ).fetchall()
        if not rows:
            break
        vectors = embedder.embed([paper_text(row["title"], row["abstract"]) for row in rows])
        with database_connection(engine) as connection:
            connection.executemany(
                "INSERT INTO paper_embeddings (arxiv_id, model, vector) VALUES (?, ?, ?) "
                "ON CONFLICT (arxiv_id) DO UPDATE SET model = excluded.model, vector = excluded.vector",
                [(row["arxiv_id"], embedder.model, encode_vector(vector)) for row, vector in zip(rows, vectors)],
            )
        done += len(rows)
        rate = done / max(time.time() - started, 1e-6)
        log(f"embeddings {done:,}/{target:,} ({100 * done // max(target, 1)}%), "
            f"about {math.ceil((target - done) / max(rate, 1e-6) / 60)} min left")
    return {"embedded": done, "model": embedder.model, "remaining": remaining - done}


# ----------------------------------------------------------------------------- stage 2: subtopics

def label_clusters(documents: list[str], labels: np.ndarray, cluster_count: int, top: int = 8) -> list[list[str]]:
    """Class-based TF-IDF: the terms that are frequent in one group but rare in the others."""
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, CountVectorizer

    joined = [" ".join(documents[index] for index in np.flatnonzero(labels == cluster)) for cluster in range(cluster_count)]
    stop_words = sorted(ENGLISH_STOP_WORDS | ACADEMIC_STOP_WORDS)
    try:
        vectorizer = CountVectorizer(stop_words=stop_words, ngram_range=(1, 2), min_df=1, max_features=40000,
                                     token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9\-]{2,}\b")
        counts = vectorizer.fit_transform(joined).toarray().astype(np.float32)
    except ValueError:  # every document was stop words
        return [[] for _ in range(cluster_count)]
    terms = vectorizer.get_feature_names_out()
    term_frequency = counts / np.maximum(counts.sum(axis=1, keepdims=True), 1)
    average_words = counts.sum() / max(cluster_count, 1)
    idf = np.log1p(average_words / np.maximum(counts.sum(axis=0), 1))
    scores = term_frequency * idf
    results = []
    for cluster in range(cluster_count):
        chosen: list[str] = []
        for index in np.argsort(scores[cluster])[::-1]:
            term = str(terms[index])
            if scores[cluster, index] <= 0:
                break
            # Skip a word already inside a chosen phrase, and a phrase made of chosen words.
            if any(term in existing.split() or existing in term.split() for existing in chosen):
                continue
            chosen.append(term)
            if len(chosen) == top:
                break
        results.append(chosen)
    return results


def readable_label(terms: list[str]) -> str:
    if not terms:
        return "General"
    words = [term[:1].upper() + term[1:] for term in terms[:3]]
    return " · ".join(words)


def subtopic_count(paper_count: int) -> int:
    if paper_count < 12:
        return 1
    return int(min(MAX_SUBTOPICS, max(2, round(math.sqrt(paper_count / 30)))))


def group_vectors(vectors: np.ndarray, cluster_count: int) -> tuple[np.ndarray, np.ndarray]:
    if cluster_count == 1:
        return np.zeros(len(vectors), dtype=int), _normalize(vectors.mean(axis=0, keepdims=True))
    from sklearn.cluster import KMeans, MiniBatchKMeans

    if len(vectors) > 5000:
        model = MiniBatchKMeans(n_clusters=cluster_count, random_state=42, n_init=3, batch_size=2048)
    else:
        model = KMeans(n_clusters=cluster_count, random_state=42, n_init=4)
    labels = model.fit_predict(vectors)
    return labels, _normalize(model.cluster_centers_.astype(np.float32))


def _category_vectors(engine: Engine, category: str, model: str) -> tuple[list[str], list[str], np.ndarray]:
    with database_connection(engine) as connection:
        rows = connection.execute(
            "SELECT p.arxiv_id, p.title, p.abstract, e.vector FROM papers p "
            "JOIN paper_embeddings e ON e.arxiv_id = p.arxiv_id AND e.model = ? "
            "WHERE p.primary_category = ?",
            (model, category),
        ).fetchall()
    ids = [row["arxiv_id"] for row in rows]
    documents = [f"{row['title']} {row['title']} {row['abstract'][:700]}" for row in rows]
    vectors = np.array([decode_vector(row["vector"]) for row in rows], dtype=np.float32) if rows else np.zeros((0, 1))
    return ids, documents, vectors


def regroup_category(engine: Engine, category: str, model: str) -> int:
    ids, documents, vectors = _category_vectors(engine, category, model)
    if not ids:
        return 0
    cluster_count = min(subtopic_count(len(ids)), len(ids))
    labels, centroids = group_vectors(_normalize(vectors), cluster_count)
    term_lists = label_clusters(documents, labels, cluster_count)
    distances = 1 - np.sum(_normalize(vectors) * centroids[labels], axis=1)
    now = _now()
    with database_connection(engine) as connection:
        connection.execute("DELETE FROM subtopics WHERE category = ?", (category,))
        connection.execute("DELETE FROM paper_subtopics WHERE category = ?", (category,))
        connection.executemany(
            "INSERT INTO subtopics (category, cluster_id, label, terms, size, centroid, model, clustered_size, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (category, cluster, readable_label(term_lists[cluster]), json.dumps(term_lists[cluster]),
                 int(np.sum(labels == cluster)), encode_vector(centroids[cluster]), model, len(ids), now)
                for cluster in range(cluster_count)
            ],
        )
        connection.executemany(
            "INSERT INTO paper_subtopics (arxiv_id, category, cluster_id, distance) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (arxiv_id) DO UPDATE SET category = excluded.category, cluster_id = excluded.cluster_id, "
            "distance = excluded.distance",
            [(arxiv_id, category, int(label), float(distance)) for arxiv_id, label, distance in zip(ids, labels, distances)],
        )
    return cluster_count


def assign_new_papers(engine: Engine, category: str, model: str) -> int:
    """Put newly embedded papers into the nearest existing subtopic (no full re-grouping)."""
    with database_connection(engine) as connection:
        clusters = connection.execute(
            "SELECT cluster_id, centroid FROM subtopics WHERE category = ? AND model = ? ORDER BY cluster_id",
            (category, model),
        ).fetchall()
        rows = connection.execute(
            "SELECT p.arxiv_id, e.vector FROM papers p JOIN paper_embeddings e ON e.arxiv_id = p.arxiv_id AND e.model = ? "
            "LEFT JOIN paper_subtopics s ON s.arxiv_id = p.arxiv_id AND s.category = p.primary_category "
            "WHERE p.primary_category = ? AND s.arxiv_id IS NULL",
            (model, category),
        ).fetchall()
    if not clusters or not rows:
        return 0
    centroids = np.array([decode_vector(row["centroid"]) for row in clusters])
    vectors = _normalize(np.array([decode_vector(row["vector"]) for row in rows]))
    similarity = vectors @ centroids.T
    best = similarity.argmax(axis=1)
    with database_connection(engine) as connection:
        connection.executemany(
            "INSERT INTO paper_subtopics (arxiv_id, category, cluster_id, distance) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (arxiv_id) DO UPDATE SET category = excluded.category, cluster_id = excluded.cluster_id, "
            "distance = excluded.distance",
            [
                (row["arxiv_id"], category, int(clusters[index]["cluster_id"]), float(1 - similarity[position, index]))
                for position, (row, index) in enumerate(zip(rows, best))
            ],
        )
        for position, cluster in enumerate(clusters):
            added = int(np.sum(best == position))
            if added:
                connection.execute(
                    "UPDATE subtopics SET size = size + ? WHERE category = ? AND cluster_id = ?",
                    (added, category, cluster["cluster_id"]),
                )
    return len(rows)


def cluster_papers(
    engine: Engine,
    model: str | None = None,
    recluster: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    ensure_tables(engine)
    with database_connection(engine) as connection:
        if model is None:
            row = connection.execute(
                "SELECT model, COUNT(*) AS n FROM paper_embeddings GROUP BY model ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {"categories": 0, "regrouped": 0, "assigned": 0, "detail": "No embeddings yet; run the embed stage."}
            model = row["model"]
        embedded = {
            row["category"]: row["n"]
            for row in connection.execute(
                "SELECT p.primary_category AS category, COUNT(*) AS n FROM papers p "
                "JOIN paper_embeddings e ON e.arxiv_id = p.arxiv_id AND e.model = ? GROUP BY p.primary_category",
                (model,),
            ).fetchall()
        }
        grouped = {
            row["category"]: row
            for row in connection.execute(
                "SELECT category, MAX(clustered_size) AS clustered_size, MAX(model) AS model FROM subtopics GROUP BY category"
            ).fetchall()
        }
    regrouped = assigned = 0
    for category, count in sorted(embedded.items()):
        previous = grouped.get(category)
        stale = (
            recluster
            or previous is None
            or previous["model"] != model
            or count > previous["clustered_size"] * (1 + RECLUSTER_GROWTH)
            or (previous["clustered_size"] < 12 <= count)
        )
        if stale:
            clusters = regroup_category(engine, category, model)
            regrouped += 1
            log(f"subtopics {category}: {count:,} papers in {clusters} groups")
        else:
            assigned += assign_new_papers(engine, category, model)
    if assigned:
        log(f"subtopics: placed {assigned:,} new papers into existing groups")
    _invalidate_caches()
    return {"categories": len(embedded), "regrouped": regrouped, "assigned": assigned, "model": model}


# ----------------------------------------------------------------------------- stage 3: summaries

def configure_llm(base_url: str | None = None, model: str | None = None) -> str:
    """Default to a free local Ollama model unless another provider is configured."""
    if base_url:
        os.environ["AI_BASE_URL"] = base_url
    elif not os.environ.get("AI_BASE_URL"):
        os.environ["AI_BASE_URL"] = DEFAULT_LOCAL_LLM_URL
    chosen = model or os.environ.get("SUMMARY_MODEL") or os.environ.get("AI_CHAT_MODEL") or DEFAULT_LOCAL_LLM
    os.environ["AI_CHAT_MODEL"] = chosen
    return chosen


def summarize_papers(
    engine: Engine,
    limit: int | None = None,
    deadline: float | None = None,
    workers: int = 1,
    retry_failed: bool = False,
    summarizer: Callable[[dict[str, Any]], dict[str, str]] | None = None,
    model_name: str | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    ensure_tables(engine)
    if summarizer is None:
        from backend.ai_service import generate_research_analysis

        summarizer = generate_research_analysis
    model_name = model_name or os.environ.get("AI_CHAT_MODEL", "")
    status_filter = "r.arxiv_id IS NULL" if not retry_failed else "(r.arxiv_id IS NULL OR r.status = 'failed')"
    written = failed = 0
    started = time.time()
    consecutive_failures = 0
    while (limit is None or written + failed < limit) and (deadline is None or time.time() < deadline):
        size = max(1, min(workers * 4, (limit - written - failed) if limit is not None else workers * 4))
        with database_connection(engine) as connection:
            rows = connection.execute(
                "SELECT p.arxiv_id, p.title, p.abstract, p.analysis FROM papers p "
                "LEFT JOIN paper_summary_runs r ON r.arxiv_id = p.arxiv_id "
                f"WHERE {status_filter} AND p.approval_status <> 'rejected' "
                "ORDER BY CASE WHEN p.approval_status = 'approved' THEN 0 ELSE 1 END, p.published_at DESC, p.arxiv_id DESC "
                "LIMIT ?",
                (size,),
            ).fetchall()
        if not rows:
            break

        def run(row: Any) -> tuple[Any, dict[str, str] | None, str | None]:
            try:
                if json.loads(row["analysis"] or "{}").get("summary"):
                    return row, None, "already summarized"  # written by an admin or an earlier run
                paper = {"title": row["title"], "abstract": row["abstract"]}
                try:
                    return row, summarizer(paper), None
                except Exception:  # small local models sometimes return broken JSON; ask once more
                    return row, summarizer(paper), None
            except Exception as error:  # one bad answer must not stop the batch
                detail = getattr(error, "detail", None) or str(error)
                return row, None, str(detail)[:300]

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(run, rows))
        for row, generated, error in results:
            if error == "already summarized":
                with database_connection(engine) as connection:
                    connection.execute(
                        "INSERT INTO paper_summary_runs (arxiv_id, status, model, error, updated_at) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (arxiv_id) DO NOTHING",
                        (row["arxiv_id"], "done", "existing", None, _now()),
                    )
                continue
            with database_connection(engine) as connection:
                if generated:
                    current = connection.execute(
                        "SELECT analysis FROM papers WHERE arxiv_id = ?", (row["arxiv_id"],)
                    ).fetchone()
                    analysis = json.loads(current["analysis"] or "{}") if current else {}
                    analysis.update(generated)
                    analysis["summary_generated_at"] = _now()
                    analysis["summary_model"] = model_name
                    connection.execute(
                        "UPDATE papers SET analysis = ? WHERE arxiv_id = ?",
                        (json.dumps(analysis, ensure_ascii=False), row["arxiv_id"]),
                    )
                    written += 1
                    consecutive_failures = 0
                else:
                    failed += 1
                    consecutive_failures += 1
                connection.execute(
                    "INSERT INTO paper_summary_runs (arxiv_id, status, model, error, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (arxiv_id) DO UPDATE SET status = excluded.status, model = excluded.model, "
                    "error = excluded.error, updated_at = excluded.updated_at",
                    (row["arxiv_id"], "done" if generated else "failed", model_name, error, _now()),
                )
        rate = (written + failed) / max(time.time() - started, 1e-6)
        log(f"summaries {written:,} written, {failed:,} failed · {rate * 60:.1f} papers/min")
        if consecutive_failures >= 10 and written == 0:
            last_error = results[-1][2]
            log(f"Stopping: the model failed 10 times in a row. Last error: {last_error}")
            break
    return {"summarized": written, "failed": failed, "model": model_name}


# ----------------------------------------------------------------------------- status / CLI

def status(engine: Engine) -> dict[str, Any]:
    ensure_tables(engine)
    with database_connection(engine) as connection:
        total = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        embedded = connection.execute("SELECT COUNT(*) FROM paper_embeddings").fetchone()[0]
        grouped = connection.execute("SELECT COUNT(*) FROM paper_subtopics").fetchone()[0]
        subtopic_total = connection.execute("SELECT COUNT(*) FROM subtopics").fetchone()[0]
        summarized = connection.execute("SELECT COUNT(*) FROM paper_summary_runs WHERE status = 'done'").fetchone()[0]
        failed = connection.execute("SELECT COUNT(*) FROM paper_summary_runs WHERE status = 'failed'").fetchone()[0]
        documented = connection.execute("SELECT COUNT(*) FROM papers WHERE institution IS NOT NULL").fetchone()[0]
    pct = (lambda value: round(100 * value / total, 1) if total else 0.0)
    return {
        "total_papers": total,
        "embedded": embedded, "embedded_pct": pct(embedded),
        "grouped": grouped, "subtopics": subtopic_total,
        "summarized": summarized, "summarized_pct": pct(summarized), "summary_failures": failed,
        "documented_affiliations": documented, "documented_pct": pct(documented),
    }


def _invalidate_caches() -> None:
    try:
        from backend import subtopics

        subtopics.invalidate()
    except Exception:
        pass
    try:
        from backend import research_gaps

        research_gaps.invalidate_cache()
    except Exception:
        pass


def _stages(name: str) -> Iterable[str]:
    return {"all": ("embed", "cluster", "summarize"), "daily": ("embed", "cluster", "summarize")}.get(name, (name,))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Embed, group and summarize papers with free local models.")
    parser.add_argument("stage", choices=["all", "daily", "embed", "cluster", "summarize", "status"])
    parser.add_argument("--limit", type=int, default=None, help="Summarize/embed at most this many papers.")
    parser.add_argument("--max-minutes", type=float, default=None, help="Stop starting new work after this long.")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("SUMMARY_WORKERS", "1")),
                        help="Parallel summary requests (1 for a laptop; more for a hosted API).")
    parser.add_argument("--model", default=None, help=f"Chat model for summaries (default {DEFAULT_LOCAL_LLM} on Ollama).")
    parser.add_argument("--base-url", default=None, help=f"OpenAI-compatible URL (default {DEFAULT_LOCAL_LLM_URL}).")
    parser.add_argument("--recluster", action="store_true", help="Re-group every category from scratch.")
    parser.add_argument("--retry-failed", action="store_true", help="Try papers whose summary failed before.")
    args = parser.parse_args(argv)

    engine = create_database_engine()
    if args.stage == "status":
        print(json.dumps(status(engine), indent=2))
        return
    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    results: dict[str, Any] = {}
    try:
        for stage in _stages(args.stage):
            if deadline and time.time() >= deadline:
                break
            if stage == "embed":
                results["embed"] = embed_papers(engine, limit=None if args.stage in {"all", "daily"} else args.limit,
                                                deadline=deadline, log=lambda m: print(m, file=sys.stderr, flush=True))
            elif stage == "cluster":
                results["cluster"] = cluster_papers(engine, recluster=args.recluster,
                                                    log=lambda m: print(m, file=sys.stderr, flush=True))
            elif stage == "summarize":
                model = configure_llm(args.base_url, args.model)
                print(f"Summarizing with {model} at {os.environ['AI_BASE_URL']}", file=sys.stderr, flush=True)
                results["summarize"] = summarize_papers(
                    engine, limit=args.limit, deadline=deadline, workers=args.workers,
                    retry_failed=args.retry_failed, model_name=model,
                    log=lambda m: print(m, file=sys.stderr, flush=True),
                )
    except KeyboardInterrupt:
        print("Stopped. Run the same command again to continue where it left off.", file=sys.stderr)
    results["status"] = status(engine)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
