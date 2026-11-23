"""Loopback-only bearer gateway for the Render backend's Ollama requests."""

import json
import logging
import os
import re
import secrets
from http.client import HTTPConnection, HTTPException as UpstreamError
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

MAX_REQUEST_BYTES = 2 * 1024 * 1024
PORT = 11435
LOGGER = logging.getLogger("ollama_gateway")
STATE_DIR = Path(os.environ.get(
    "RDN_OLLAMA_GATEWAY_DIR", Path.home() / ".config" / "rdn-ollama-gateway"
))


def load_token() -> str:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_file = STATE_DIR / "bearer-token"
    try:
        with token_file.open("x") as output:
            output.write(secrets.token_hex(32) + "\n")
    except FileExistsError:
        pass
    token = token_file.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError(f"Invalid gateway token in {token_file}")
    return token


def compact_archive_chat(body: bytes) -> bytes:
    """Bound inference work for the archive assistant on a CPU."""
    separator = "\n\nArchive context (JSON):\n"
    try:
        payload = json.loads(body)
        messages = payload["messages"]
        first, last = messages[0], messages[-1]
        if first["role"] != "system" or not first["content"].startswith(
            "You are the research assistant for the Research Discovery Network,"
        ) or last["role"] != "user":
            return body
        question, archive = last["content"].split(separator, 1)
        context = json.loads(archive)
        papers = []
        for paper in context["retrieved_papers"][:3]:
            excerpt = paper.get("reviewed_summary") or paper.get("abstract") or ""
            papers.append({
                "arxiv_id": paper["arxiv_id"],
                "title": paper["title"],
                "year": paper["year"],
                "topic": paper["topic"],
                "excerpt": excerpt[:320],
            })
        context["retrieved_papers"] = papers
        history = [
            {**turn, "content": turn["content"][:300]} for turn in messages[1:-1][-2:]
        ]
        last = {**last, "content": question + separator + json.dumps(
            context, ensure_ascii=False, separators=(",", ":")
        )}
        payload["messages"] = [first, *history, last]
        payload["max_tokens"] = min(payload.get("max_tokens") or 140, 140)
        return json.dumps(payload, ensure_ascii=False).encode()
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return body


def proxy(method: str, path: str, body: bytes) -> Response:
    if path == "/v1/chat/completions":
        body = compact_archive_chat(body)
    connection = HTTPConnection("127.0.0.1", 11434, timeout=85)
    try:
        connection.request(method, path, body or None, {"Content-Type": "application/json"})
        upstream = connection.getresponse()
        return Response(upstream.read(), upstream.status, media_type="application/json")
    except (OSError, UpstreamError) as exc:
        raise HTTPException(502, "Ollama request failed") from exc
    finally:
        connection.close()


def create_app(token: str) -> FastAPI:
    app = FastAPI(openapi_url=None, redirect_slashes=False)
    allowed = {
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/embeddings"),
    }
    expected = f"Bearer {token}".encode("ascii")

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def forward(request: Request) -> Response:
        method, path = request.method, request.url.path
        supported = (method, path) in allowed
        route = path if supported else "(unsupported route)"
        LOGGER.info("Incoming %s %s", method, route)
        headers = [value for name, value in request.scope["headers"] if name == b"authorization"]
        if len(headers) != 1 or not secrets.compare_digest(headers[0], expected):
            LOGGER.info("%s %s → 401", method, route)
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

        if not supported:
            LOGGER.info("%s %s → 404", method, route)
            raise HTTPException(404, "Not found")

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                LOGGER.info("%s %s → 413", method, route)
                raise HTTPException(413, "Request too large")
            body.extend(chunk)

        try:
            response = await run_in_threadpool(proxy, method, path, bytes(body))
        except HTTPException as exc:
            LOGGER.info("%s %s → %s", method, route, exc.status_code)
            raise
        LOGGER.info("%s %s → %s", method, route, response.status_code)
        return response

    return app


if __name__ == "__main__":
    os.umask(0o077)
    token = load_token()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(STATE_DIR / "requests.log")],
    )
    uvicorn.run(create_app(token), host="127.0.0.1", port=PORT, access_log=False)
