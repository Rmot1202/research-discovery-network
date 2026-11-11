"""Helpers shared by the dataset builder and the evaluator."""

from __future__ import annotations

import json
import re
from typing import Any

CITATION = re.compile(r"\[(?:arXiv|OpenAlex):\s*([A-Za-z0-9./_-]+?)\]")
REFUSAL_HINTS = (
    "do not answer", "don't answer", "does not answer", "doesn't answer", "do not cover",
    "don't cover", "does not cover", "doesn't cover", "not in the retrieved", "no retrieved paper",
    "none of the retrieved", "couldn't find", "could not find", "try searching", "try a search",
)


def cite(paper_id: str) -> str:
    return f"[OpenAlex:{paper_id}]" if paper_id.startswith("W") else f"[arXiv:{paper_id}]"


def cited_ids(answer: str) -> list[str]:
    return [match.strip() for match in CITATION.findall(answer)]


def context_ids(messages: list[dict[str, str]]) -> set[str]:
    """IDs of the papers that were actually given to the model in the last user turn."""
    content = messages[-1]["content"] if messages[-1]["role"] == "user" else messages[-2]["content"]
    _, _, payload = content.partition("Archive context (JSON):\n")
    try:
        context = json.loads(payload)
    except json.JSONDecodeError:
        return set()
    return {paper["arxiv_id"] for paper in context.get("retrieved_papers", [])}


def looks_like_refusal(answer: str) -> bool:
    lowered = answer.lower()
    return any(hint in lowered for hint in REFUSAL_HINTS)


def first_sentences(text: str, count: int = 2, limit: int = 420) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    joined = " ".join(sentences[:count]).strip()
    return joined if len(joined) <= limit else joined[: limit - 3].rsplit(" ", 1)[0] + "..."


def check_answer(answer: str, messages: list[dict[str, str]], answerable: bool) -> dict[str, Any]:
    allowed = context_ids(messages)
    cites = cited_ids(answer)
    invented = [paper_id for paper_id in cites if paper_id not in allowed]
    return {
        "citations": len(cites),
        "invented_citations": invented,
        "refused": looks_like_refusal(answer),
        "ok": (not invented) and ((bool(cites) and not looks_like_refusal(answer)) if answerable else looks_like_refusal(answer)),
    }
