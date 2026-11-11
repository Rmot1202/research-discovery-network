from __future__ import annotations

import re


def normalize_arxiv_id(identifier: str) -> str:
    return re.sub(r"v\d+$", "", identifier.rsplit("/", 1)[-1])


OLD_STYLE_NUMBER = re.compile(r"\d{7}")
# Papers imported from OpenAlex are stored under their OpenAlex work ID (W1234567890) in the
# same arxiv_id column, so every table and query keeps working unchanged.
OPENALEX_ID = re.compile(r"W\d+")


def is_openalex_id(paper_id: str) -> bool:
    return bool(OPENALEX_ID.fullmatch(paper_id or ""))


def arxiv_path(arxiv_id: str, categories: list[str] | None = None, primary_category: str | None = None) -> str:
    """The identifier arXiv's own URLs need.

    Papers from before April 2007 have old-style IDs such as cs/0002216 or math/0002216.
    normalize_arxiv_id keeps only the number (0002216), which arXiv does not recognize, so the
    archive is restored from the paper's first listed category (the archive it was filed in).
    """
    if "/" in arxiv_id or not OLD_STYLE_NUMBER.fullmatch(arxiv_id):
        return arxiv_id
    first = (categories[0] if categories else None) or primary_category or "cs"
    return f"{first.split('.')[0]}/{arxiv_id}"


def abs_url(arxiv_id: str, categories: list[str] | None = None, primary_category: str | None = None) -> str:
    if is_openalex_id(arxiv_id):
        return f"https://openalex.org/works/{arxiv_id}"
    return f"https://arxiv.org/abs/{arxiv_path(arxiv_id, categories, primary_category)}"


def pdf_url(arxiv_id: str, categories: list[str] | None = None, primary_category: str | None = None) -> str:
    if is_openalex_id(arxiv_id):
        return f"https://openalex.org/works/{arxiv_id}"  # serialize_paper prefers the open-access PDF
    return f"https://arxiv.org/pdf/{arxiv_path(arxiv_id, categories, primary_category)}"
