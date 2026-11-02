"""Discovery Score: how underrepresented a paper's topic, institution and country are
inside the imported arXiv dataset.

This measures REPRESENTATION (how often something appears in our arXiv collection), not
VISIBILITY (whether the work gets attention). Citations, downloads and other attention
signals are not used here; they are planned for a later version.

Field
    The paper's primary arXiv category (for example cs.CV). Every factor compares the paper
    with other papers in that same field.

Three factors, each normalized to 0-100 within the field
    topic        How common the paper's subtopic is among the subtopics of its field
                 (subtopics come from the clustering stage in backend/analysis_pipeline.py).
                 Until a field has been clustered, the fallback compares the field itself with
                 the other arXiv CS categories ("topic_basis": "category").
    institution  How common the paper's documented institution is within the field.
    geography    How common the paper's documented country is within the field.

Normalization (same for every factor)
    score = 100 * (ln(max) - ln(count)) / (ln(max) - ln(min))
    where count is how many papers in the field share the paper's group, and min/max are the
    smallest and largest group sizes in that field. 100 = the least common group in the field,
    0 = the most common. The log scale stops one dominant group from pushing everyone else
    to the top. If every group is the same size the score is 0. A factor is left out (None)
    when the value is unknown or the field has fewer than MIN_FIELD_SAMPLE documented papers.

Discovery Score
    Weighted average of the available factors (default topic 40%, institution 30%,
    geography 30%). Missing factors are left out and the remaining weights are rescaled, and
    "coverage" says how much of the full weight was available.

A paper does not need to score high on all three. It is flagged as underrepresented when the
Discovery Score reaches OVERALL_THRESHOLD, or when any single factor reaches FACTOR_THRESHOLD
(for example a common topic from a rarely seen country).
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy.engine import Engine

from backend.database import database_connection

FACTORS = ("topic", "institution", "geography")
DEFAULT_WEIGHTS = {"topic": 0.4, "institution": 0.3, "geography": 0.3}
FACTOR_THRESHOLD = float(os.environ.get("DISCOVERY_FACTOR_THRESHOLD", "75"))
OVERALL_THRESHOLD = float(os.environ.get("DISCOVERY_OVERALL_THRESHOLD", "60"))
MIN_FIELD_SAMPLE = int(os.environ.get("DISCOVERY_MIN_FIELD_SAMPLE", "20"))
CACHE_SECONDS = int(os.environ.get("DISCOVERY_CACHE_SECONDS", "600"))
MEASURES = (
    "Representation inside the imported arXiv dataset only. This is not a visibility measure: "
    "citations, downloads and other attention signals are not included."
)
FACTOR_LABELS = {
    "topic": "Underrepresented topic",
    "institution": "Underrepresented institution",
    "geography": "Underrepresented country or region",
}


def parse_weights(value: str | dict[str, float] | None = None) -> dict[str, float]:
    """Read weights like "topic=0.4,institution=0.3,geography=0.3" (or 40,30,30).

    Missing factors keep their default. Weights are rescaled to sum to 1.
    """
    if value is None:
        value = os.environ.get("DISCOVERY_WEIGHTS", "")
    weights = dict(DEFAULT_WEIGHTS)
    if isinstance(value, dict):
        items = value.items()
    else:
        items = []
        for part in str(value).split(","):
            if "=" in part:
                key, number = part.split("=", 1)
                items.append((key, number))
    for key, number in items:
        key = str(key).strip().lower()
        if key in ("geo", "country", "region"):
            key = "geography"
        if key not in FACTORS:
            raise ValueError(f"Unknown Discovery Score factor: {key}")
        number = float(number)
        if number < 0 or math.isnan(number):
            raise ValueError("Weights must be zero or positive.")
        weights[key] = number
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("At least one weight must be above zero.")
    return {key: weights[key] / total for key in FACTORS}


def normalized_scores(counts: Counter[str]) -> dict[str, float]:
    """Log-scaled min-max: least common group -> 100, most common -> 0."""
    if not counts:
        return {}
    high, low = max(counts.values()), min(counts.values())
    if high == low:
        return {group: 0.0 for group in counts}
    span = math.log(high) - math.log(low)
    return {group: round(100 * (math.log(high) - math.log(count)) / span, 1) for group, count in counts.items()}


def _clean(value: Any) -> str | None:
    text = " ".join(str(value or "").split()).lower()
    return text or None


class DiscoveryIndex:
    """Group sizes for every field, built once from light columns and cached."""

    def __init__(self, rows: list[tuple[str, str, str | None, str | None, int | None]]) -> None:
        self.papers: dict[str, tuple[str, str | None, str | None, int | None]] = {}
        category_counts: Counter[str] = Counter()
        subtopic_counts: dict[str, Counter[str]] = defaultdict(Counter)
        institution_counts: dict[str, Counter[str]] = defaultdict(Counter)
        country_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for arxiv_id, field, institution, country, cluster in rows:
            institution, country = _clean(institution), _clean(country)
            self.papers[arxiv_id] = (field, institution, country, cluster)
            category_counts[field] += 1
            if cluster is not None:
                subtopic_counts[field][str(cluster)] += 1
            if institution:
                institution_counts[field][institution] += 1
            if country:
                country_counts[field][country] += 1
        self.field_sizes = dict(category_counts)
        self.category_scores = normalized_scores(category_counts)
        self.subtopic_scores = self._per_field(subtopic_counts)
        self.institution_scores = self._per_field(institution_counts)
        self.country_scores = self._per_field(country_counts)
        self.subtopic_counts = subtopic_counts
        self.institution_counts = institution_counts
        self.country_counts = country_counts
        self.subtopic_labels: dict[tuple[str, str], str] = {}

    @staticmethod
    def _per_field(counts: dict[str, Counter[str]]) -> dict[str, dict[str, float]]:
        return {
            field: normalized_scores(groups)
            for field, groups in counts.items()
            if sum(groups.values()) >= MIN_FIELD_SAMPLE and len(groups) > 1
        }

    def factors(self, field: str, institution: Any, country: Any, cluster: int | None) -> dict[str, Any]:
        institution, country = _clean(institution), _clean(country)
        if cluster is not None and field in self.subtopic_scores:
            topic, basis = self.subtopic_scores[field].get(str(cluster)), "subtopic"
        else:
            topic, basis = self.category_scores.get(field), "category"
        return {
            "topic": topic,
            "institution": self.institution_scores.get(field, {}).get(institution) if institution else None,
            "geography": self.country_scores.get(field, {}).get(country) if country else None,
            "topic_basis": basis,
        }


def combine(factors: dict[str, Any], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Weighted Discovery Score plus flags; works with any subset of factors."""
    weights = weights or parse_weights()
    available = {key: factors.get(key) for key in FACTORS if factors.get(key) is not None}
    coverage = sum(weights[key] for key in available)
    score = (
        round(sum(available[key] * weights[key] for key in available) / coverage, 1)
        if coverage > 0
        else None
    )
    flags = [key for key, value in available.items() if value >= FACTOR_THRESHOLD]
    reasons = [FACTOR_LABELS[key] for key in flags]
    if score is not None and score >= OVERALL_THRESHOLD and not reasons:
        reasons.append("High combined score")
    return {
        "discovery_score": score,
        "factors": {key: factors.get(key) for key in FACTORS},
        "topic_basis": factors.get("topic_basis"),
        "weights": {key: round(weights[key], 4) for key in FACTORS},
        "coverage": round(coverage, 4),
        "flags": flags,
        "reasons": reasons,
        "underrepresented": bool(reasons),
        "measures": "representation",
    }


_cache_lock = threading.Lock()
_cache: dict[int, tuple[float, DiscoveryIndex]] = {}


def invalidate() -> None:
    with _cache_lock:
        _cache.clear()


def load_index(engine: Engine, *, fresh: bool = False) -> DiscoveryIndex:
    key = id(engine)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not fresh and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    with database_connection(engine) as connection:
        try:
            rows = connection.execute(
                "SELECT p.arxiv_id, p.primary_category, p.institution, p.country, s.cluster_id "
                "FROM papers p LEFT JOIN paper_subtopics s "
                "ON s.arxiv_id = p.arxiv_id AND s.category = p.primary_category"
            ).fetchall()
        except Exception:  # the clustering tables do not exist yet
            rows = [
                (*row, None)
                for row in connection.execute(
                    "SELECT arxiv_id, primary_category, institution, country FROM papers"
                ).fetchall()
            ]
        try:
            labels = {
                (row[0], str(row[1])): row[2]
                for row in connection.execute("SELECT category, cluster_id, label FROM subtopics").fetchall()
            }
        except Exception:
            labels = {}
    index = DiscoveryIndex([tuple(row[i] for i in range(5)) for row in rows])
    index.subtopic_labels = labels
    with _cache_lock:
        _cache[key] = (time.monotonic(), index)
    return index


def score_paper(
    index: DiscoveryIndex,
    *,
    arxiv_id: str,
    field: str,
    institution: Any,
    country: Any,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    known = index.papers.get(arxiv_id)
    cluster = known[3] if known else None
    return combine(index.factors(field, institution, country, cluster), weights)


def ranked_ids(
    index: DiscoveryIndex,
    arxiv_ids: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Papers sorted by Discovery Score (highest first); papers with no score go last."""
    weights = weights or parse_weights()
    ids = arxiv_ids if arxiv_ids is not None else list(index.papers)
    scored = []
    for arxiv_id in ids:
        known = index.papers.get(arxiv_id)
        if not known:
            continue
        field, institution, country, cluster = known
        scored.append((arxiv_id, combine(index.factors(field, institution, country, cluster), weights)))
    scored.sort(key=lambda item: (item[1]["discovery_score"] is None, -(item[1]["discovery_score"] or 0), -item[1]["coverage"]))
    return scored


def field_report(index: DiscoveryIndex, field: str, top: int = 8) -> dict[str, Any]:
    """Least represented subtopics, institutions and countries in one field."""

    def least(
        counts: dict[str, Counter[str]], scores: dict[str, dict[str, float]], subtopics: bool = False
    ) -> list[dict[str, Any]]:
        groups = counts.get(field, Counter())
        field_scores = scores.get(field, {})
        rows = [
            {
                "name": index.subtopic_labels.get((field, name), f"Subtopic {name}") if subtopics else name,
                "papers": count,
                "score": field_scores.get(name),
            }
            for name, count in groups.items()
        ]
        rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0), row["name"]))
        return rows[:top]

    return {
        "field": field,
        "papers": index.field_sizes.get(field, 0),
        "measures": MEASURES,
        "documented": {
            "subtopic": sum(index.subtopic_counts.get(field, Counter()).values()),
            "institution": sum(index.institution_counts.get(field, Counter()).values()),
            "geography": sum(index.country_counts.get(field, Counter()).values()),
        },
        "least_represented": {
            "subtopics": least(index.subtopic_counts, index.subtopic_scores, subtopics=True),
            "institutions": least(index.institution_counts, index.institution_scores),
            "countries": least(index.country_counts, index.country_scores),
        },
        "category_score": index.category_scores.get(field),
    }
