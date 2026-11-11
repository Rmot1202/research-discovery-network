"""Score a model on the held-out set (training/data/val.jsonl) through any OpenAI-compatible API.

Run it twice, before and after fine-tuning, and compare:
  python -m training.eval --base-url http://127.0.0.1:11434/v1 --model qwen2.5:1.5b
  python -m training.eval --base-url http://127.0.0.1:11434/v1 --model rdn-assistant

Metrics
  grounded_rate     answerable questions answered with >=1 citation and no invented citation
  refusal_rate      unanswerable questions where the model said the papers don't cover it
  invented_rate     answers citing an ID that was not in the retrieved papers (hallucination)
  avg_citations     citations per answer
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

from training.common import check_answer


def chat(base_url: str, model: str, api_key: str, messages: list[dict[str, str]]) -> str:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps({"model": model, "temperature": 0.2, "messages": messages}).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read())["choices"][0]["message"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="training/data/val.jsonl")
    parser.add_argument("--base-url", default=os.environ.get("AI_BASE_URL", "http://127.0.0.1:11434/v1"))
    parser.add_argument("--model", default=os.environ.get("AI_CHAT_MODEL", "rdn-assistant"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--show", type=int, default=3, help="Print this many sample answers.")
    args = parser.parse_args()
    api_key = os.environ.get("AI_API_KEY", "")

    rows = [json.loads(line) for line in Path(args.data).open(encoding="utf-8")][: args.limit]
    answerable = unanswerable = grounded = refused = invented = citations = 0
    for index, row in enumerate(rows):
        prompt = row["messages"][:-1]
        answer = chat(args.base_url, args.model, api_key, prompt)
        result = check_answer(answer, prompt, row["meta"]["answerable"])
        citations += result["citations"]
        invented += bool(result["invented_citations"])
        if row["meta"]["answerable"]:
            answerable += 1
            grounded += result["ok"]
        else:
            unanswerable += 1
            refused += result["ok"]
        if index < args.show:
            print(f"--- {row['meta']['kind']} ---\n{answer}\n")
    total = len(rows) or 1
    print(json.dumps({
        "model": args.model,
        "examples": len(rows),
        "grounded_rate": round(grounded / max(answerable, 1), 3),
        "refusal_rate": round(refused / max(unanswerable, 1), 3),
        "invented_rate": round(invented / total, 3),
        "avg_citations": round(citations / total, 2),
    }, indent=2))


if __name__ == "__main__":
    main()
