from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException

from backend.arxiv_utils import arxiv_path, normalize_arxiv_id


def ai_endpoint(resource: str) -> tuple[str, dict[str, str]]:
    base_url = os.environ.get("AI_BASE_URL", "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=503, detail="Configure AI_BASE_URL to enable AI analysis.")
    parsed_url = urllib.parse.urlparse(base_url)
    local_http = parsed_url.scheme == "http" and parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed_url.scheme != "https" and not local_http:
        raise HTTPException(status_code=503, detail="AI_BASE_URL must use HTTPS, except for a local HTTP model server.")
    if not os.environ.get("AI_API_KEY") and not local_http:
        raise HTTPException(status_code=503, detail="Configure AI_API_KEY for the selected AI service.")
    headers = {"Content-Type": "application/json"}
    if api_key := os.environ.get("AI_API_KEY"):
        headers["Authorization"] = f"Bearer {api_key}"
    return f"{base_url}/{resource.lstrip('/')}", headers


def ai_request(resource: str, payload: dict[str, Any]) -> dict[str, Any]:
    endpoint, headers = ai_endpoint(resource)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read())
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=502, detail=f"AI provider request failed: {error}") from error
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="AI provider returned an invalid response.")
    return result


def generate_embeddings(text_values: list[str]) -> list[list[float]]:
    model = os.environ.get("AI_EMBEDDING_MODEL", "")
    if not model:
        raise HTTPException(status_code=503, detail="Configure AI_EMBEDDING_MODEL to enable embeddings.")
    result = ai_request("embeddings", {"model": model, "input": [value[:12000] for value in text_values]})
    try:
        rows = sorted(result["data"], key=lambda row: row["index"])
        embeddings = [[float(value) for value in row["embedding"]] for row in rows]
        if len(embeddings) != len(text_values) or not embeddings or any(
            len(embedding) != len(embeddings[0]) for embedding in embeddings
        ):
            raise ValueError("Embedding response count or dimensions do not match the request.")
        return embeddings
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="AI provider returned invalid embeddings.") from error


def generate_research_analysis(paper: dict[str, Any]) -> dict[str, str]:
    model = os.environ.get("AI_CHAT_MODEL", "")
    if not model:
        raise HTTPException(status_code=503, detail="Configure AI_CHAT_MODEL to enable summaries.")
    prompt = (
        "Analyze the supplied research paper metadata. Use only statements supported by the title "
        "and abstract. Do not infer author identity, demographics, affiliation, country, or intent. "
        "Return one JSON object with concise string fields: summary (plain-language, 2-3 sentences), "
        "key_contribution, application, relevance (why it may be useful to readers), topics (comma-separated "
        "research topics), methods (comma-separated methods), and context (comma-separated context/application areas). "
        "If information is not stated, say 'Not stated in the abstract'."
    )
    result = ai_request(
        "chat/completions",
        {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps({"title": paper["title"], "abstract": paper["abstract"]}),
                },
            ],
        },
    )
    try:
        return coerce_research_analysis(result["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="AI provider returned an invalid research summary.") from error


ANALYSIS_FIELDS = ("summary", "key_contribution", "application", "relevance", "topics", "methods", "context")
NOT_STATED = "Not stated in the abstract"


def coerce_research_analysis(content: str) -> dict[str, str]:
    """Parse a model's JSON answer leniently. Small local models often wrap JSON in code
    fences, return lists instead of comma-separated strings, or skip an empty field."""
    text_value = content.strip()
    fenced = re.search(r"\{.*\}", text_value, re.DOTALL)
    if fenced:
        text_value = fenced.group(0)
    analysis = json.loads(text_value)
    if not isinstance(analysis, dict):
        raise ValueError("The response is not a JSON object.")
    lowered = {str(key).strip().lower().replace(" ", "_"): value for key, value in analysis.items()}
    cleaned: dict[str, str] = {}
    for field in ANALYSIS_FIELDS:
        value = lowered.get(field)
        if isinstance(value, list):
            value = ", ".join(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, dict):
            value = ", ".join(str(item).strip() for item in value.values() if str(item).strip())
        elif value is not None and not isinstance(value, str):
            value = str(value)
        cleaned[field] = (value or "").strip()[:1200] or NOT_STATED
    if cleaned["summary"] == NOT_STATED:
        raise ValueError("The response has no summary.")
    return cleaned


def extract_pdf_affiliations(arxiv_id: str, title: str) -> dict[str, Any]:
    try:
        import pymupdf
    except ImportError as error:
        raise HTTPException(status_code=503, detail="Install the PDF extraction dependency to inspect paper affiliations.") from error
    safe_id = arxiv_path(normalize_arxiv_id(arxiv_id))  # restores cs/ for pre-2007 IDs
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})", safe_id):
        raise HTTPException(status_code=422, detail="This arXiv identifier cannot be used to retrieve a PDF.")
    pdf_url = f"https://arxiv.org/pdf/{safe_id}"
    request = urllib.request.Request(
        pdf_url,
        headers={"User-Agent": "ResearchDiscoveryMVP/0.3 (academic metadata enrichment)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            pdf_bytes = response.read(20_000_001)
        if len(pdf_bytes) > 20_000_000:
            raise HTTPException(status_code=413, detail="The paper PDF exceeds the 20 MB enrichment limit.")
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            source_text = "\n".join(page.get_text() for page in document[:2])
        finally:
            document.close()
    except HTTPException:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=f"Could not retrieve or parse the arXiv PDF: {error}") from error
    if not source_text.strip():
        raise HTTPException(status_code=422, detail="No selectable text was found on the paper's first two pages.")

    model = os.environ.get("AI_CHAT_MODEL", "")
    if not model:
        raise HTTPException(status_code=503, detail="Configure AI_CHAT_MODEL to extract PDF metadata.")
    prompt = (
        "Extract only institution and country affiliations explicitly printed in the supplied paper text. "
        "Do not infer an affiliation from an author name, email domain, or external knowledge. "
        "Return JSON with a candidates array; each item must have institution, country, and evidence_quote "
        "string fields. Country may be empty if not explicitly stated. Evidence_quote must be an exact "
        "verbatim excerpt from the supplied text. Return an empty array when no explicit affiliation is present."
    )
    result = ai_request(
        "chat/completions",
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"title": title, "paper_text": source_text[:16000]})},
            ],
        },
    )
    try:
        parsed = json.loads(result["choices"][0]["message"]["content"])
        candidates = parsed["candidates"]
        if not isinstance(candidates, list):
            raise ValueError("The candidate list is invalid.")
        normalized_source = " ".join(source_text.split()).casefold()
        verified = []
        for candidate in candidates[:8]:
            institution = candidate["institution"].strip()
            country = candidate["country"].strip()
            evidence = " ".join(candidate["evidence_quote"].split())
            if (
                not institution
                or institution.casefold() not in evidence.casefold()
                or not evidence
                or evidence.casefold() not in normalized_source
            ):
                continue
            verified.append(
                {
                    "institution": institution[:300],
                    "country": country[:120] if country and country.casefold() in evidence.casefold() else None,
                    "evidence": evidence[:2000],
                    "source_url": pdf_url,
                }
            )
        return {"arxiv_id": safe_id, "suggestions": verified, "source_url": pdf_url}
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="AI provider returned invalid affiliation suggestions.") from error
