"""Build a fine-tuning dataset for the Research Discovery Network assistant.

Every example uses the exact prompt the live app sends (backend.paper_chat.build_chat_messages):
system prompt + "Question: ...\n\nArchive context (JSON): {...retrieved_papers...}".
The model therefore learns the job it actually does in production: read retrieved papers,
answer only from them, cite [arXiv:ID] / [OpenAlex:ID], and say so when the papers don't help.

Example types (mixed automatically):
  summary     "What is <title> about?"            -> grounded summary with a citation
  search      "Find papers on <phrase>"           -> short answer + bulleted cited list
  authors     "Who wrote <title>?"                -> authors from metadata, cited
  compare     "How do <A> and <B> differ?"        -> two cited papers contrasted
  affiliation "Which country are the authors of <title> from?" -> declines to guess from names
  negative    question whose paper is NOT in the context -> says the papers don't answer it

Answers come from templates by default (fast, no GPU, always grounded). With --teacher you can
have a bigger local model (for example Ollama qwen2.5:7b) write richer answers; every teacher
answer is checked and dropped if it cites a paper that was not in the context.

Usage (from the repo root, with the same DATABASE_URL the app uses or the local SQLite default):
  python -m training.build_dataset --examples 3000 --out training/data
  python -m training.build_dataset --examples 3000 --teacher http://127.0.0.1:11434/v1 --teacher-model qwen2.5:7b
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import text

from backend import main as app_main
from backend.paper_chat import build_chat_messages, collection_stats, retrieve_papers
from backend.research_gaps import gap_summary_for_chat
from backend.taxonomy import CATEGORY_LABELS
from training.common import check_answer, cite, first_sentences

TYPE_WEIGHTS = {"summary": 0.28, "search": 0.27, "authors": 0.1, "compare": 0.12, "affiliation": 0.08, "negative": 0.15}
TITLE_STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "with", "to", "via", "using", "towards", "toward", "from", "by", "is", "are"}


def sample_papers(limit: int, seed: int) -> list[dict[str, Any]]:
    """Random papers with real abstracts. Uses analysis summaries when the app already made them."""
    order = "RANDOM()"
    with app_main.ENGINE.connect() as connection:
        rows = connection.execute(text(
            "SELECT arxiv_id, title, abstract, authors, published_at, primary_category, analysis "
            f"FROM papers WHERE length(abstract) > 300 ORDER BY {order} LIMIT :limit"
        ), {"limit": limit}).mappings().all()
    papers = []
    for row in rows:
        try:
            authors = json.loads(row["authors"] or "[]")
        except json.JSONDecodeError:
            authors = []
        try:
            analysis = json.loads(row["analysis"] or "{}")
        except json.JSONDecodeError:
            analysis = {}
        papers.append({**row, "authors": authors if isinstance(authors, list) else [], "summary": analysis.get("summary")})
    random.Random(seed).shuffle(papers)
    return papers


def title_phrase(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9-]+", title) if w.lower() not in TITLE_STOP]
    return " ".join(words[: random.randint(2, 4)]).lower() or title.lower()


def retrieved(question: str, must_include: dict[str, Any] | None = None, exclude: str | None = None, limit: int = 5):
    papers, _ = retrieve_papers(app_main.ENGINE, question, limit=limit + 2)
    papers = [p for p in papers if p["arxiv_id"] != exclude]
    if must_include and all(p["arxiv_id"] != must_include["arxiv_id"] for p in papers):
        hit = retrieve_papers(app_main.ENGINE, must_include["title"], limit=3)[0]
        target = next((p for p in hit if p["arxiv_id"] == must_include["arxiv_id"]), None)
        if target is None:
            return None
        papers = [target] + papers
    return papers[:limit]


def bullet(paper: dict[str, Any]) -> str:
    gist = paper.get("summary") or first_sentences(paper["abstract"], 1, 260)
    return f"- {paper['title']} {cite(paper['arxiv_id'])} ({(paper['published_at'] or '')[:4]}): {gist}"


def make_example(kind: str, paper: dict[str, Any], other: dict[str, Any]) -> tuple[str, list[dict], str, bool] | None:
    title = paper["title"]
    if kind == "summary":
        question = random.choice([f"What is the paper \"{title}\" about?", f"Summarize \"{title}\".", f"Explain the main idea of {title}."])
        papers = retrieved(title, must_include=paper)
        if not papers:
            return None
        target = next(p for p in papers if p["arxiv_id"] == paper["arxiv_id"])
        body = target.get("summary") or first_sentences(target["abstract"], 3, 600)
        answer = f"{title} {cite(target['arxiv_id'])} ({CATEGORY_LABELS.get(target['primary_category'], target['primary_category'])}): {body}"
        return question, papers, answer, True
    if kind == "search":
        phrase = title_phrase(title)
        question = random.choice([f"Find papers on {phrase}.", f"What research is there about {phrase}?", f"Any recent work on {phrase}?"])
        papers = retrieved(question)
        if not papers:
            return None
        top = papers[:3]
        answer = f"The archive has {len(papers)} closely matching papers on {phrase}; the most relevant are:\n" + "\n".join(bullet(p) for p in top)
        return question, papers, answer, True
    if kind == "authors":
        question = f"Who wrote \"{title}\"?"
        papers = retrieved(title, must_include=paper)
        if not papers:
            return None
        target = next(p for p in papers if p["arxiv_id"] == paper["arxiv_id"])
        names = target["authors"][:8]
        if not names:
            return None
        more = " and others" if len(target["authors"]) > 8 else ""
        answer = f"{title} {cite(target['arxiv_id'])} lists these authors: {', '.join(names)}{more}."
        return question, papers, answer, True
    if kind == "compare":
        question = f"How do \"{title}\" and \"{other['title']}\" differ?"
        first = retrieved(title, must_include=paper, limit=3)
        second = retrieved(other["title"], must_include=other, limit=3)
        if not first or not second:
            return None
        a = next(p for p in first if p["arxiv_id"] == paper["arxiv_id"])
        b = next(p for p in second if p["arxiv_id"] == other["arxiv_id"])
        papers = [a, b] + [p for p in first + second if p["arxiv_id"] not in {a["arxiv_id"], b["arxiv_id"]}][:3]
        answer = (
            "They tackle different problems.\n"
            f"- {a['title']} {cite(a['arxiv_id'])} ({CATEGORY_LABELS.get(a['primary_category'], '')}): {first_sentences(a['abstract'], 1, 260)}\n"
            f"- {b['title']} {cite(b['arxiv_id'])} ({CATEGORY_LABELS.get(b['primary_category'], '')}): {first_sentences(b['abstract'], 1, 260)}"
        )
        return question, papers, answer, True
    if kind == "affiliation":
        question = random.choice([f"Which country are the authors of \"{title}\" from?", f"What university wrote \"{title}\"?"])
        papers = retrieved(title, must_include=paper)
        if not papers:
            return None
        answer = (
            f"The archive context for {title} {cite(paper['arxiv_id'])} lists author names but no affiliations, "
            "and I won't guess an institution or country from names. Check the paper's PDF or the "
            "admin affiliation scan for verified affiliations."
        )
        return question, papers, answer, True
    if kind == "negative":
        question = random.choice([f"What does \"{title}\" conclude?", f"Summarize the paper \"{title}\"."])
        papers = retrieved(other["title"], exclude=paper["arxiv_id"])
        if not papers or any(p["arxiv_id"] == paper["arxiv_id"] for p in papers):
            return None
        answer = (
            f"The retrieved papers don't cover \"{title}\", so I can't summarize it without guessing. "
            f"Try searching for its exact title or a key phrase such as \"{title_phrase(title)}\"."
        )
        return question, papers, answer, False
    raise ValueError(kind)


def teacher_answer(base_url: str, model: str, messages: list[dict[str, str]]) -> str | None:
    payload = json.dumps({"model": model, "temperature": 0.3, "messages": messages}).encode()
    request = urllib.request.Request(f"{base_url.rstrip('/')}/chat/completions", data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())["choices"][0]["message"]["content"].strip()
    except Exception as error:  # noqa: BLE001 - teacher is optional; fall back to the template
        print(f"  teacher failed: {error}", file=sys.stderr)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--examples", type=int, default=3000)
    parser.add_argument("--out", default="training/data")
    parser.add_argument("--val-fraction", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--teacher", help="OpenAI-compatible base URL of a bigger model, e.g. http://127.0.0.1:11434/v1")
    parser.add_argument("--teacher-model", default="qwen2.5:7b")
    args = parser.parse_args()
    random.seed(args.seed)

    app_main.init_db()
    stats = collection_stats(app_main.connect_db)
    try:
        stats["research_gap_scores"] = gap_summary_for_chat(app_main.ENGINE)
    except Exception:  # noqa: BLE001 - gap scores are optional context
        stats["research_gap_scores"] = []
    pool = sample_papers(args.examples * 2, args.seed)
    if len(pool) < 20:
        sys.exit("Need at least 20 papers with abstracts in the database.")

    kinds = list(TYPE_WEIGHTS)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    counts = {kind: 0 for kind in kinds}
    teacher_kept = teacher_dropped = 0
    for index, paper in enumerate(pool):
        if len(rows) >= args.examples:
            break
        kind = random.choices(kinds, weights=[TYPE_WEIGHTS[k] for k in kinds])[0]
        other = pool[(index + len(pool) // 2) % len(pool)]
        built = make_example(kind, paper, other)
        if built is None:
            continue
        question, papers, answer, answerable = built
        messages = build_chat_messages(question, [], papers, stats, None)
        if args.teacher and kind in {"summary", "search", "compare"}:
            better = teacher_answer(args.teacher, args.teacher_model, messages)
            if better and check_answer(better, messages, answerable)["ok"]:
                answer, teacher_kept = better, teacher_kept + 1
            else:
                teacher_dropped += 1
        assert check_answer(answer, messages, answerable)["ok"], (kind, answer)
        rows.append({"messages": messages + [{"role": "assistant", "content": answer}],
                     "meta": {"kind": kind, "answerable": answerable, "paper": paper["arxiv_id"]}})
        counts[kind] += 1
        if len(rows) % 100 == 0:
            print(f"  {len(rows)}/{args.examples} examples")

    random.shuffle(rows)
    split = max(1, int(len(rows) * args.val_fraction))
    for name, part in (("val.jsonl", rows[:split]), ("train.jsonl", rows[split:])):
        with (out_dir / name).open("w", encoding="utf-8") as handle:
            for row in part:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"train": len(rows) - split, "val": split, "by_type": counts,
                      "teacher_kept": teacher_kept, "teacher_dropped": teacher_dropped, "out": str(out_dir)}, indent=2))


if __name__ == "__main__":
    main()
