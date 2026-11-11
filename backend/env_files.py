"""Load local env files so `neon deploy` / `neon env pull` output works without exporting anything.

Order (first wins): real environment variables (Render, GitHub Actions, your shell),
then `.env.local` (written by the Neon CLI), then `.env`. Existing variables are never
overridden, so hosted settings always beat local files.

Set RDN_SKIP_ENV_FILES=1 to skip this (the tests do, so they never touch Neon).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_loaded = False


def _parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = _LINE.match(raw)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def load_env_files(root: Path = ROOT) -> None:
    global _loaded
    if _loaded or os.environ.get("RDN_SKIP_ENV_FILES") == "1":
        return
    _loaded = True
    for name in (".env.local", ".env"):
        path = root / name
        if path.is_file():
            for key, value in _parse(path).items():
                if value and key not in os.environ:
                    os.environ[key] = value
