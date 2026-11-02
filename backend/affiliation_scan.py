"""Paper scan: fill in missing institution / school / country affiliations for every
paper in the archive, from documented sources only.

arXiv's own metadata almost never includes affiliations, so the scan walks the whole
archive in arXiv-ID order and looks each paper up in two places:

1. OpenAlex (default, fast). arXiv papers have DataCite DOIs (10.48550/arXiv.<id>);
   OpenAlex links each author to the institutions printed on the paper, with ROR IDs,
   country codes, and institution type (education, company, government, ...). 50 papers
   are looked up per request.
2. The paper's own PDF (optional fallback, slow). Page 1 is read and lines that name a
   university, institute, school, lab, or company are kept verbatim as evidence. arXiv
   asks for at most one request every 3 seconds, so this is throttled.

Nothing is guessed from author names or email domains. Each saved affiliation keeps
its source URL, the verbatim affiliation text, and the method that found it, and is
marked as not yet reviewed. Existing institution/country values (for example, ones an
admin entered) are never overwritten.

Which sources are used is set by AFFILIATION_SOURCES (or --sources):
  "pdf" (default): only the paper's own PDF. Free, no account or key, about 1 paper / 3 s.
  "openalex+pdf": OpenAlex first, PDF for misses.   "openalex": OpenAlex only.

Run it from the admin dashboard, the API, or the command line:

    python -m backend.affiliation_scan --limit 2000          # PDF only (default)
    python -m backend.affiliation_scan --sources openalex+pdf
    python -m backend.affiliation_scan --status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

OPENALEX_URL = "https://api.openalex.org/works"
OPENALEX_BATCH = 50
PDF_INTERVAL_SECONDS = 3.0
USER_AGENT = "ResearchDiscoveryNetwork/0.4 (paper affiliation scan)"
COUNTRY_NAMES: dict[str, str] = json.loads(
    (Path(__file__).parent / "data" / "country_codes.json").read_text(encoding="utf-8")
)
INSTITUTION_WORDS = re.compile(
    r"\b(universit(?:y|ies|é|e|at|ät|à|ad|ade|eit)|institut(?:e|o|ion)?|college|school|academy|"
    r"laborator(?:y|ies)|lab|polytechnic|polytechnique|politecnico|hochschule|center|centre|"
    r"research|independent researcher|corporation|inc\.|ltd|gmbh|hospital|national|ministry)\b",
    re.IGNORECASE,
)
STRONG_WORDS = re.compile(r"\b(universit\w*|institut\w*|college|school|laborator\w*|department|dept\.|faculty|independent researcher)\b", re.IGNORECASE)
NOT_AFFILIATION = re.compile(r"(abstract|introduction|arxiv|copyright|preprint|keywords|classification|©|\. \. \.)", re.IGNORECASE)
TOP_LEVEL = re.compile(r"\b(universit\w*|college|polytechn\w*|politecnico|hochschule|institute of technology|independent researcher)\b", re.IGNORECASE)
PROSE = re.compile(r"\b(we|our|us|this paper|this work|propose[sd]?|study|studies|show|shows|using|is|are|was|were)\b", re.IGNORECASE)
EMAIL_OR_URL = re.compile(r"\S+@\S+|https?://\S+|www\.\S+")
COUNTRY_ALIASES = {"USA": "United States", "U.S.A.": "United States", "UK": "United Kingdom", "P.R. China": "China",
                   "PR China": "China", "Korea": "South Korea", "Republic of Korea": "South Korea"}

def configured_sources() -> tuple[bool, bool]:
    """(use_openalex, use_pdf) from AFFILIATION_SOURCES; default is PDF only, no OpenAlex."""
    value = os.environ.get("AFFILIATION_SOURCES", "pdf").strip().lower()
    use_openalex = "openalex" in value
    use_pdf = "pdf" in value or not use_openalex
    return use_openalex, use_pdf


_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


# ----------------------------------------------------------------------------- state

def _ensure_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS affiliation_scan_state (id INTEGER PRIMARY KEY, state_json TEXT NOT NULL)"
        ))


def default_state() -> dict[str, Any]:
    return {
        "status": "not_started", "cursor": "", "checked": 0, "found": 0, "found_openalex": 0,
        "found_pdf": 0, "remaining": None, "pdf_fallback": False, "error": None,
        "started_at": None, "updated_at": None, "last_found": [],
    }


def read_state(engine: Engine) -> dict[str, Any]:
    _ensure_tables(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT state_json FROM affiliation_scan_state WHERE id = 1")).first()
    state = default_state()
    if row:
        state.update(json.loads(row[0]))
    return state


def write_state(engine: Engine, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(state, ensure_ascii=False)
    with engine.begin() as connection:
        updated = connection.execute(
            text("UPDATE affiliation_scan_state SET state_json = :s WHERE id = 1"), {"s": payload}
        ).rowcount
        if not updated:
            connection.execute(text("INSERT INTO affiliation_scan_state (id, state_json) VALUES (1, :s)"), {"s": payload})


def progress(engine: Engine) -> dict[str, Any]:
    state = read_state(engine)
    with engine.connect() as connection:
        total = connection.execute(text("SELECT COUNT(*) FROM papers")).scalar_one()
        documented = connection.execute(text("SELECT COUNT(*) FROM papers WHERE institution IS NOT NULL")).scalar_one()
        countries = connection.execute(text("SELECT COUNT(*) FROM papers WHERE country IS NOT NULL")).scalar_one()
    state.update({
        "total_papers": total,
        "documented_papers": documented,
        "documented_countries": countries,
        "documented_pct": round(100 * documented / total, 1) if total else 0.0,
        "is_running": bool(_thread and _thread.is_alive()),
    })
    return state


# ----------------------------------------------------------------------------- sources

class OpenAlexBudgetExhausted(RuntimeError):
    pass


def _http_json(url: str, timeout: int = 40) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if os.environ.get("OPENALEX_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['OPENALEX_API_KEY']}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 429:
                body = error.read().decode("utf-8", "replace")
                if "budget" in body.lower():
                    raise OpenAlexBudgetExhausted(
                        "OpenAlex daily budget used up (resets at midnight UTC). Add a free OPENALEX_API_KEY "
                        "to .env for a larger budget, or use the PDF fallback."
                    ) from error
            if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 3)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError("unreachable")


def _arxiv_doi(arxiv_id: str) -> str:
    return f"10.48550/arxiv.{re.sub(r'v[0-9]+$', '', arxiv_id).lower()}"


def fetch_openalex(arxiv_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return OpenAlex works keyed by arXiv ID for one batch of up to 50 papers."""
    by_doi = {_arxiv_doi(arxiv_id): arxiv_id for arxiv_id in arxiv_ids}
    params = {
        "filter": "doi:" + "|".join(by_doi),
        "select": "id,doi,authorships",
        "per-page": str(OPENALEX_BATCH),
    }
    if os.environ.get("OPENALEX_EMAIL"):
        params["mailto"] = os.environ["OPENALEX_EMAIL"]
    data = _http_json(f"{OPENALEX_URL}?{urllib.parse.urlencode(params, safe='|:/')}")
    works = {}
    for work in data.get("results", []):
        doi = (work.get("doi") or "").lower().replace("https://doi.org/", "")
        if doi in by_doi:
            works[by_doi[doi]] = work
    return works


def affiliations_from_openalex(work: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per (author, institution) pair that OpenAlex linked to the paper."""
    results = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("raw_author_name") or (authorship.get("author") or {}).get("display_name") or ""
        raw = [" ".join(value.split()) for value in authorship.get("raw_affiliation_strings") or [] if value]
        for institution in authorship.get("institutions") or []:
            name = (institution.get("display_name") or "").strip()
            if not name:
                continue
            code = institution.get("country_code")
            stem = re.sub(r"\s*\(.*\)$", "", name).casefold()
            evidence = next((value for value in raw if stem and stem in value.casefold()), raw[0] if raw else name)
            results.append({
                "author": author,
                "institution": name,
                "country": COUNTRY_NAMES.get(code or "", None),
                "country_code": code,
                "type": institution.get("type"),
                "ror": institution.get("ror"),
                "evidence": evidence[:500],
            })
    return results


def pdf_first_page_text(arxiv_id: str) -> str:
    import pymupdf  # PyMuPDF is already a backend dependency

    request = urllib.request.Request(f"https://arxiv.org/pdf/{arxiv_id}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        pdf_bytes = response.read(20_000_001)
    if len(pdf_bytes) > 20_000_000:
        return ""
    document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if not len(document):
            return ""
        first = document[0].get_text()
        # Some fields (notably maths) print author addresses at the very end of the paper.
        last = document[len(document) - 1].get_text() if len(document) > 1 else ""
        return first + "\n\f\n" + "\n".join(last.splitlines()[-40:])
    finally:
        document.close()


def affiliations_from_text(page_text: str) -> list[dict[str, Any]]:
    """Verbatim affiliation lines: the header of page 1 (before the abstract) with the usual
    institution words, plus footnote/end-of-paper lines that use unambiguous words
    (university, institute, department, ...). Emails and URLs are removed, never used."""
    first, _, last = page_text.partition("\n\f\n")
    marker = re.search(r"\n\s*(?:abstract|a b s t r a c t)\b", first, re.IGNORECASE)
    head_part, rest = (first[:marker.start()], first[marker.end():]) if marker else (first[:3000], "")
    head = head_part[:4000].splitlines()
    tail = rest.splitlines()[-30:] + last.splitlines()
    country_lookup = {name.casefold(): name for name in COUNTRY_NAMES.values()}
    country_lookup.update({alias.casefold(): name for alias, name in COUNTRY_ALIASES.items()})
    found, seen = [], set()
    candidates = [(line, INSTITUTION_WORDS, head[index + 1] if index + 1 < len(head) else "") for index, line in enumerate(head)]
    candidates += [(line, STRONG_WORDS, tail[index + 1] if index + 1 < len(tail) else "") for index, line in enumerate(tail)]
    for line, words, next_line in candidates:
        clean = " ".join(EMAIL_OR_URL.sub("", re.sub(r"^[\d∗*†‡§¶,\s]+", "", line)).split()).strip(" ,;")
        if not (8 <= len(clean) <= 220) or not words.search(clean) or NOT_AFFILIATION.search(clean) \
                or PROSE.search(clean):
            continue
        parts = [part.strip(" .;") for part in clean.split(",")]
        institution = next((part for part in parts if TOP_LEVEL.search(part)), None) \
            or next((part for part in parts if words.search(part)), clean)
        country = next((country_lookup[part.casefold()] for part in reversed(parts) if part.casefold() in country_lookup), None)
        if country is None and next_line.strip(" .,;").casefold() in country_lookup:
            country = country_lookup[next_line.strip(" .,;").casefold()]
            clean = f"{clean}, {next_line.strip()}"
        key = institution.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append({"author": None, "institution": institution[:300], "country": country, "country_code": None,
                      "type": None, "ror": None, "evidence": clean})
    return found[:12]


# ----------------------------------------------------------------------------- saving

def _primary(affiliations: list[dict[str, Any]]) -> tuple[str, str | None]:
    """The first-listed institution is the paper's primary affiliation; its country is the
    first one documented for that institution. Counts across all authors are kept in analysis."""
    institution = affiliations[0]["institution"]
    country = next((item["country"] for item in affiliations if item["institution"] == institution and item["country"]), None)
    return institution, country


def save_affiliations(engine: Engine, arxiv_id: str, affiliations: list[dict[str, Any]], method: str, source_url: str) -> bool:
    if not affiliations:
        return False
    institution, country = _primary(affiliations)
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT analysis, institution FROM papers WHERE arxiv_id = :id"), {"id": arxiv_id}
        ).first()
        if row is None or row[1]:
            return False  # never overwrite a value that is already documented
        analysis = json.loads(row[0] or "{}")
        institutions = Counter(item["institution"] for item in affiliations)
        analysis.update({
            "affiliations": affiliations[:40],
            "institutions_all": [name for name, _ in institutions.most_common(15)],
            "countries_all": sorted({item["country"] for item in affiliations if item["country"]}),
            "has_school_affiliation": any(item.get("type") == "education" for item in affiliations)
            or any(re.search(r"universit|college|school|polytechn|hochschule", item["institution"], re.I) for item in affiliations),
            "affiliation_source_url": source_url,
            "affiliation_evidence": affiliations[0]["evidence"],
            "affiliation_method": method,
            "affiliation_reviewed": False,
            "affiliation_scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        connection.execute(
            text("UPDATE papers SET institution = :i, country = :c, analysis = :a WHERE arxiv_id = :id AND institution IS NULL"),
            {"i": institution, "c": country, "a": json.dumps(analysis, ensure_ascii=False), "id": arxiv_id},
        )
    return True


# ----------------------------------------------------------------------------- scan loop

def _next_batch(engine: Engine, cursor: str, size: int) -> list[str]:
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(
            text("SELECT arxiv_id FROM papers WHERE institution IS NULL AND arxiv_id > :c ORDER BY arxiv_id LIMIT :n"),
            {"c": cursor, "n": size},
        ).all()]


def run_scan(
    engine: Engine,
    limit: int | None = None,
    pdf_fallback: bool = False,
    on_batch: Callable[[dict[str, Any]], None] | None = None,
    stop: threading.Event | None = None,
    use_openalex: bool | None = None,
) -> dict[str, Any]:
    stop = stop or threading.Event()
    if use_openalex is None:
        use_openalex, configured_pdf = configured_sources()
        pdf_fallback = pdf_fallback or configured_pdf
    if not use_openalex:
        pdf_fallback = True  # the PDF is the only source left
    state = read_state(engine)
    if state["status"] == "complete":
        state.update(default_state())  # start a fresh pass; already-documented papers are skipped anyway
    state.update({"status": "running", "error": None, "pdf_fallback": pdf_fallback, "openalex_exhausted": False,
                  "started_at": state.get("started_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")})
    write_state(engine, state)
    checked_this_run = 0
    try:
        while not stop.is_set() and (limit is None or checked_this_run < limit):
            size = OPENALEX_BATCH if limit is None else min(OPENALEX_BATCH, limit - checked_this_run)
            batch = _next_batch(engine, state["cursor"], size)
            if not batch:
                state["status"] = "complete"
                break
            try:
                works = {} if (state.get("openalex_exhausted") or not use_openalex) else fetch_openalex(batch)
            except OpenAlexBudgetExhausted as error:
                if not pdf_fallback:
                    state["status"] = "paused"
                    state["error"] = str(error)
                    break
                state["openalex_exhausted"] = True  # keep going with PDFs only for this run
                state["error"] = str(error)
                works = {}
            for arxiv_id in batch:
                if stop.is_set():
                    break
                work = works.get(arxiv_id)
                found = False
                if work:
                    affiliations = affiliations_from_openalex(work)
                    found = save_affiliations(engine, arxiv_id, affiliations, "openalex", work["id"])
                    if found:
                        state["found_openalex"] += 1
                if not found and pdf_fallback:
                    try:
                        page = pdf_first_page_text(arxiv_id)
                        found = save_affiliations(engine, arxiv_id, affiliations_from_text(page), "pdf_text",
                                                  f"https://arxiv.org/pdf/{arxiv_id}")
                        if found:
                            state["found_pdf"] += 1
                    except Exception as error:  # one bad PDF must not stop the scan
                        state["error"] = f"{arxiv_id}: {error}"[:300]
                    time.sleep(PDF_INTERVAL_SECONDS)
                if found:
                    state["found"] += 1
                    state["last_found"] = ([arxiv_id] + state["last_found"])[:8]
                state["checked"] += 1
                state["cursor"] = arxiv_id
                checked_this_run += 1
            write_state(engine, state)
            _invalidate_gap_cache()
            if on_batch:
                on_batch(state)
            time.sleep(0.2)  # stay polite to OpenAlex
        if state["status"] == "running":
            state["status"] = "paused"
    except Exception as error:
        state["status"] = "error"
        state["error"] = str(error)[:500]
    with engine.connect() as connection:
        state["remaining"] = connection.execute(
            text("SELECT COUNT(*) FROM papers WHERE institution IS NULL AND arxiv_id > :c"), {"c": state["cursor"]}
        ).scalar_one()
    write_state(engine, state)
    _invalidate_gap_cache()
    return state


def scan_pdfs_for(engine: Engine, arxiv_ids: list[str], log: Callable[[str], None] | None = None) -> dict[str, int]:
    """PDF-only affiliation lookup for specific papers (used by the daily update). Each paper's
    PDF is read once; papers already checked are skipped on later runs."""
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS affiliation_pdf_checked (arxiv_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL)"
        ))
        done = {row[0] for row in connection.execute(text("SELECT arxiv_id FROM affiliation_pdf_checked")).all()}
    checked = found = 0
    for arxiv_id in arxiv_ids:
        if arxiv_id in done:
            continue
        try:
            page = pdf_first_page_text(arxiv_id)
            if save_affiliations(engine, arxiv_id, affiliations_from_text(page), "pdf_text",
                                 f"https://arxiv.org/pdf/{arxiv_id}"):
                found += 1
        except Exception as error:  # one bad PDF must not stop the run
            if log:
                log(f"  {arxiv_id}: {error}")
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO affiliation_pdf_checked (arxiv_id, checked_at) VALUES (:id, :t) ON CONFLICT DO NOTHING"),
                {"id": arxiv_id, "t": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            )
        checked += 1
        if log and checked % 25 == 0:
            log(f"  PDFs read {checked:,}, affiliations found {found:,}")
        time.sleep(PDF_INTERVAL_SECONDS)
    _invalidate_gap_cache()
    return {"pdf_checked": checked, "pdf_found": found}


def _invalidate_gap_cache() -> None:
    try:
        from . import research_gaps
        research_gaps.invalidate_cache()
    except Exception:
        pass


def start_background(engine: Engine, pdf_fallback: bool = False, limit: int | None = None) -> dict[str, Any]:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return progress(engine)
        _stop.clear()
        _thread = threading.Thread(target=run_scan, args=(engine, limit, pdf_fallback, None, _stop), daemon=True)
        _thread.start()
    time.sleep(0.2)
    return progress(engine)


def pause_background(engine: Engine) -> dict[str, Any]:
    _stop.set()
    if _thread:
        _thread.join(timeout=60)
    return progress(engine)


def mark_interrupted(engine: Engine) -> None:
    """On API start-up, a scan that was running when the server stopped becomes 'paused'."""
    state = read_state(engine)
    if state["status"] == "running":
        state["status"] = "paused"
        state["error"] = "API restarted; resume the scan to continue from its saved cursor."
        write_state(engine, state)


def main() -> None:
    from .database import create_database_engine

    parser = argparse.ArgumentParser(description="Scan papers for documented institution/school/country affiliations.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after checking this many papers (default: all).")
    parser.add_argument("--pdf", action="store_true", help="Also read page 1 of the PDF when OpenAlex has nothing (slow).")
    parser.add_argument("--sources", default=None, help='"pdf" (default), "openalex+pdf", or "openalex".')
    parser.add_argument("--status", action="store_true", help="Print scan progress and exit.")
    parser.add_argument("--restart", action="store_true", help="Start again from the first paper.")
    args = parser.parse_args()
    if args.sources:
        os.environ["AFFILIATION_SOURCES"] = args.sources
    engine = create_database_engine()
    _ensure_tables(engine)
    if args.status:
        print(json.dumps(progress(engine), indent=2))
        return
    if args.restart:
        write_state(engine, default_state())

    def report(state: dict[str, Any]) -> None:
        print(f"checked {state['checked']:,} · found {state['found']:,} "
              f"(OpenAlex {state['found_openalex']:,}, PDF {state['found_pdf']:,}) · cursor {state['cursor']}", flush=True)

    try:
        final = run_scan(engine, args.limit, args.pdf, report)
    except KeyboardInterrupt:
        final = read_state(engine)
        final["status"] = "paused"
        write_state(engine, final)
    print(json.dumps({key: final[key] for key in ("status", "checked", "found", "remaining", "error")}, indent=2))


if __name__ == "__main__":
    main()
