# Training your own research assistant

This kit fine-tunes a small open model to be the archive's assistant, so the site does not need
OpenAI or any paid API.

## How the assistant works (and what training changes)

The assistant is retrieval-augmented (RAG). For every question the backend
(`backend/paper_chat.py`) searches the whole archive, puts the best 5–8 papers into the prompt as
JSON, and the model answers from them.

| Part | Where it comes from | Retrain when papers change? |
|---|---|---|
| Facts about papers | retrieval from the database, at question time | no, new papers are searchable as soon as they're imported |
| How to answer: short answer, bulleted list, `[arXiv:ID]` / `[OpenAlex:ID]` citations, saying "the papers don't cover this", not guessing affiliations from names | fine-tuning | only if you change the answer style |

So the model is not trained to memorize papers (a 1–3B model can't hold hundreds of thousands
of them, and it would go stale every day). It's trained to read the context well. Small base models
are the ones that need this most: out of the box they often skip citations or invent paper IDs.

## Files

| File | What it does | Runs on |
|---|---|---|
| `build_dataset.py` | makes `data/train.jsonl` and `data/val.jsonl` from your database, using the app's exact prompt (`build_chat_messages`) | your Mac or any machine with `DATABASE_URL` |
| `train_lora.py` | LoRA fine-tune with Unsloth, exports a GGUF | free GPU notebook (Colab T4 / Kaggle) |
| `Modelfile` | turns the GGUF into an Ollama model `rdn-assistant` | wherever Ollama runs |
| `eval.py` | scores any model on the held-out set: grounded rate, refusal rate, invented-citation rate | anywhere |
| `common.py` | citation checks shared by the builder and the evaluator | |

## The dataset

Each example is a full chat: system prompt, `Question: ... Archive context (JSON): {...}`, and the
target answer. Six kinds are mixed:

| Kind | Share | Teaches |
|---|---|---|
| summary | 28% | summarize a retrieved paper, cite it |
| search | 27% | "find papers on X": one-line answer + bulleted, cited list |
| compare | 12% | contrast two cited papers |
| authors | 10% | read metadata correctly |
| affiliation | 8% | refuse to infer institution or country from names |
| negative | 15% | the asked-about paper is NOT in the context, so say so and suggest a search |

Template answers are always grounded (built straight from titles, abstracts and existing
summaries). For richer wording, pass `--teacher` to have a bigger local model write the
summary/search/compare answers. Each teacher answer is checked, and dropped if it cites a paper
that wasn't in the context.

## Step by step

1. **Build the data** (repo root, on your Mac, with the Neon `DATABASE_URL` in `.env` or exported):

   ```bash
   .venv/bin/python -m training.build_dataset --examples 3000
   # better answers, slower (needs `ollama pull qwen2.5:7b`):
   .venv/bin/python -m training.build_dataset --examples 3000 \
       --teacher http://127.0.0.1:11434/v1 --teacher-model qwen2.5:7b
   ```

   3,000 examples is a good start; 5,000–10,000 helps a little more.

2. **Get a baseline** with the untrained model so you can see the improvement later:

   ```bash
   ollama pull qwen2.5:1.5b
   .venv/bin/python -m training.eval --model qwen2.5:1.5b --limit 100
   ```

3. **Train** in a free Colab notebook (Runtime → Change runtime type → T4 GPU). Upload
   `training/data/` (or push it to a private Hugging Face dataset), then:

   ```python
   # The repo is private, so upload just the training folder (zip it on your Mac:
   #   zip -r training.zip training)  then in Colab:
   !unzip -q training.zip
   !pip install -r training/requirements.txt
   !python training/train_lora.py --data training/data --model unsloth/Qwen2.5-1.5B-Instruct --epochs 2
   ```

   Use `unsloth/Qwen2.5-3B-Instruct` if memory allows; it answers noticeably better. Download
   `outputs/rdn-assistant-gguf/*.gguf` when it finishes.

4. **Serve with Ollama**: put the GGUF next to `training/Modelfile`, fix the `FROM` line to the
   exact file name Unsloth printed, then:

   ```bash
   cd training && ollama create rdn-assistant -f Modelfile
   .venv/bin/python -m training.eval --model rdn-assistant --limit 100
   ```

   Compare with step 2. You want `invented_rate` near 0 and `grounded_rate` / `refusal_rate` up.

5. **Point the app at it** (`.env` locally, or Render environment):

   ```
   AI_BASE_URL=http://127.0.0.1:11434/v1
   AI_CHAT_MODEL=rdn-assistant
   ```

## Hosting the model for the live site

Render's free web service has 512 MB of RAM, which is too small to run even a 1.5B model, so
the model has to live somewhere else:

- **Your Mac + a tunnel (free):** run Ollama and expose it over HTTPS with a Cloudflare Tunnel;
  set `AI_BASE_URL` to the tunnel URL. The assistant works only while the Mac is on.
- **Summaries only, no live server (free):** the daily GitHub Action already runs Ollama on the
  runner for summaries. Set the `SUMMARY_MODEL` variable to your model once it's published on
  the Ollama registry (`ollama push <you>/rdn-assistant`) so summaries use it. Live chat then
  falls back to cited search results.
- **Paid GPU host:** Hugging Face Inference Endpoints, Modal, or RunPod running llama.cpp
  or vLLM. Note that Hugging Face Docker/Gradio Spaces on the free CPU tier now require a PRO
  subscription to create.

## Ideas to go further

- **Hard negatives:** add questions whose context holds similar but wrong papers (same topic,
  different method) so the model learns to say "close, but not this".
- **Reviewer edits as gold data:** when an admin edits a summary in the dashboard, export
  `(context, edited summary)` pairs as top-quality examples, then retrain every month or so.
- **Better retrieval beats a bigger model:** the eval's misses are often retrieval misses.
  Logging the retrieval rank of the target paper in `eval.py` shows which one is failing.
