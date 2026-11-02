from __future__ import annotations

import re


def normalize_arxiv_id(identifier: str) -> str:
    return re.sub(r"v\d+$", "", identifier.rsplit("/", 1)[-1])
