# Research Discovery Network MVP

This project is a human-reviewed discovery workflow for arXiv Computer Science
papers. It stores original arXiv metadata separately from derived analysis,
keeps an explainable representation score, and only publishes a paper to the
Featured collection after a reviewer approves it.

## Code layout

- `src/App.tsx` coordinates page state and data loading.
- `src/components/` contains the topic network, featured-paper section,
  admin dashboard, and shared icons.
- `src/api.ts`, `src/types.ts`, and `src/paperDiscovery.ts` hold API access,
  shared frontend types, and related-paper matching.
- `backend/main.py` configures the FastAPI app and its routes.
- `backend/ai_service.py`, `backend/paper_queries.py`, and
  `backend/taxonomy.py` hold AI enrichment, paper search/scoring, and the
  arXiv category catalog.
- `backend/schemas.py` and `backend/security.py` define request models and
  admin authorization. `backend/database.py` and
  `backend/migrate_to_postgres.py` handle persistence and migration.
- `backend/paper_chat.py` is the research assistant (search + LLM answers).
- `backend/research_gaps.py` computes the under-researched and
  underrepresented scores; `src/components/ResearchGaps.tsx` shows them.
- `backend/affiliation_scan.py` is the paper scan that fills missing
  affiliations; `src/components/AffiliationScanPanel.tsx` is its admin panel.

## Quick start (every day)

Run these in two terminals from the project folder.

Terminal 1, the API:

```sh
source .venv/bin/activate
open -a Docker                 # only if you use PostgreSQL (DATABASE_URL is set in .env)
docker compose up -d postgres  # only if you use PostgreSQL
uvicorn --env-file .env backend.main:app --port 8001 --reload
```

Terminal 2, the website:

```sh
npx pnpm install   # first time, or after package.json changes
npx pnpm dev       # opens on http://localhost:8443
```

Run the tests with `python -m unittest discover -s tests`.

## Start locally with PostgreSQL

Install the Python dependencies and create local settings:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
cp .env.example .env
```

In `.env`, replace the sample PostgreSQL password and admin token with local
values. Create a private token with:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Start PostgreSQL (Docker Desktop must be running):

```sh
docker compose up -d postgres
```

For the first migration, copy the existing local SQLite collection and its
resumable arXiv import cursor into PostgreSQL:

```sh
set -a
source .env
set +a
python -m backend.migrate_to_postgres
```

The migration is idempotent: it preserves paper metadata, analysis, topic
memberships, approval state, and import progress without replacing existing
PostgreSQL rows. The source SQLite file is retained. Start the API with the
same settings:

```sh
uvicorn --env-file .env backend.main:app --port 8001
```

In a second terminal, install and start the web app:

```sh
pnpm install
pnpm dev
```

The frontend uses `VITE_API_URL` from `.env.example`; set it to
`http://127.0.0.1:8001` if the API is hosted elsewhere.

For a quick code/test-only run before PostgreSQL is available, omit
`DATABASE_URL`; the backend uses the existing `backend/data/papers.sqlite3`.
Do not run the PostgreSQL migration until PostgreSQL is available and the
destination `DATABASE_URL` points to it.

## Free AI analysis and summaries (no OpenAlex, no paid API)

`python -m backend.analysis_pipeline` fills URD components 2, 3 and 5 with free tools:

| Stage | What it does | Tool (free) |
|---|---|---|
| `embed` | turns every paper into a vector | fastembed `BAAI/bge-small-en-v1.5` (CPU) |
| `cluster` | groups each topic's papers into subtopics with readable labels | K-Means + c-TF-IDF |
| `summarize` | summary, key contribution, why it matters, topics, methods | Ollama `qwen2.5:3b` |
| affiliations | reads institutions/countries from each paper's PDF | `python -m backend.affiliation_scan` |

Every stage is resumable: press Ctrl+C any time and run the same command again.

### Run it on your Mac (one time setup)

```sh
cd "~/Desktop/Research Discovery Network Homepage"
git pull
.venv/bin/pip install -r backend/requirements-pipeline.txt
brew install ollama
ollama serve            # leave this running in its own Terminal tab
ollama pull qwen2.5:3b  # in another tab, about 2 GB
```

Point the commands at the hosted database so the live site gets the results
(Render → database → Connections → External Database URL):

```sh
export DATABASE_URL='postgresql://...your Render External Database URL...'
.venv/bin/python -m backend.analysis_pipeline embed       # all papers, roughly 30–60 min
.venv/bin/python -m backend.analysis_pipeline cluster     # a few minutes
.venv/bin/python -m backend.analysis_pipeline summarize --max-minutes 480   # overnight, repeat as needed
.venv/bin/python -m backend.affiliation_scan              # PDF affiliations, about 1 paper / 3 s
.venv/bin/python -m backend.analysis_pipeline status      # progress
```

Summaries are the slow part (a few seconds per paper), so the newest and
editor-approved papers are done first. Use `--model qwen2.5:1.5b` for faster,
simpler summaries. To use a local copy instead of the hosted database, put
`env -u DATABASE_URL` in front of each command.

The daily GitHub update (`update-papers.yml`) then keeps up on its own: it embeds
and groups each day's new papers, reads their PDFs for affiliations, and summarizes
as many as fit in about 25 minutes with Ollama on the GitHub runner.

### Exploring deeper on the map

Click a topic to travel to it, then click it again (or press **Look inside**) to
open its subtopics. The small dots around each subtopic are papers; click one for
its summary and arXiv links. **How to use** in the top bar replays a guided demo.

## Research assistant (ask the archive)

The **Ask the archive** section answers questions about every imported paper
using retrieval-augmented generation:

1. The question is matched against the whole archive with a full-text index
   (SQLite FTS5 or a PostgreSQL GIN index, built automatically at API startup
   and kept in sync with new imports).
2. If embeddings exist (from the admin **analyze** step), the top matches are
   re-ranked by semantic similarity.
3. The best 8 papers plus collection statistics are sent to the chat model,
   which answers only from them and cites each paper as `[arXiv:ID]`.

Without a configured model, the assistant still returns the best-matching
papers (search-only mode).

Free, private option with a local model (Ollama):

```sh
brew install ollama
ollama serve                 # leave running
ollama pull llama3.1:8b      # chat model
ollama pull nomic-embed-text # optional, for embeddings
```

Then in `.env`:

```dotenv
AI_BASE_URL=http://127.0.0.1:11434/v1
AI_API_KEY=
AI_CHAT_MODEL=llama3.1:8b
AI_EMBEDDING_MODEL=nomic-embed-text
```

Or use a hosted OpenAI-compatible provider (see "Configure the AI provider").
Restart the API after editing `.env`.

Endpoints: `GET /api/chat/status` and `POST /api/chat` with
`{"question": "...", "history": [], "category": "cs.CV"}`.

## 3D topic brain

The landing map is a WebGL 3D network (`3d-force-graph` + `three`). Topics are
spheres grouped into colored brain regions inside a translucent brain shell;
links are cross-listed paper counts with moving impulses.

- Drag to rotate, scroll to zoom, right-drag to pan.
- Click the map, then use W/A/S/D (or arrow keys) to fly and Q/E to move
  down/up; hold Shift to move faster.
- Click a node to fly to it; use **Travel to a connected topic** in the side
  panel to hop along links, or **Fly-through tour** to follow the strongest
  links automatically.

## Paper scan: missing affiliations

arXiv metadata almost never lists institutions, so most papers start with “Affiliation unavailable”. The paper scan walks every paper that has no affiliation yet, in arXiv-ID order, and fills it in from documented sources:

| Step | Source | Speed | What it saves |
| --- | --- | --- | --- |
| 1 (default) | [OpenAlex](https://openalex.org) works looked up by the arXiv DOI `10.48550/arXiv.<id>` | 50 papers per request | Every author → institution link, ROR ID, country, and institution type (education, company, ...) |
| 2 (optional) | The paper's PDF: page 1 header and the last ~40 lines of the last page | 1 paper every 3 s (arXiv's limit) | Verbatim affiliation lines that name a university, institute, school, lab, or company |

Rules: values are never guessed from author names or email domains; emails and URLs are stripped from evidence; an existing institution/country (for example, one an admin entered) is never overwritten. Each paper stores `affiliation_method` (`openalex`, `pdf_text`, or `manual`), `affiliation_source_url`, the verbatim `affiliation_evidence`, every institution and country found (`institutions_all`, `countries_all`), `has_school_affiliation`, and `affiliation_reviewed: false` until an admin confirms it. Paper cards show “via OpenAlex · unreviewed” or “from PDF · unreviewed” with a link to the source.

Run it from the admin dashboard (**Paper scan** panel), or from the command line in the project folder:

```bash
.venv/bin/python -m backend.affiliation_scan --limit 2000      # OpenAlex only, first 2,000 unscanned papers
.venv/bin/python -m backend.affiliation_scan                   # whole archive (resumable; Ctrl+C pauses)
.venv/bin/python -m backend.affiliation_scan --pdf --limit 300 # also read PDFs for papers OpenAlex misses
.venv/bin/python -m backend.affiliation_scan --status          # progress
.venv/bin/python -m backend.affiliation_scan --restart         # start a new pass from the first paper
```

Prefix with `env -u DATABASE_URL` to scan the local SQLite file instead of PostgreSQL. Without `OPENALEX_API_KEY`, OpenAlex gives each network a small shared daily budget (about 1,000 requests ≈ 50,000 papers); when it runs out the scan pauses with a message and resumes from its saved cursor. Add a free key to `.env` for a larger budget.

API: `GET /api/affiliations/progress`, `POST /api/admin/affiliations/scan?pdf_fallback=false&limit=`, `POST /api/admin/affiliations/pause` (admin token required for the last two).

## Research gap scores

Two separate scores, both 0–100 where higher means more of a gap. They are computed from the imported metadata only and shown in the **Gaps** section, the 3D map (Color → Gap index), the topic detail panel, and the research assistant's context.

### Score 1: under-researched (how little work exists)

| Component | Weight | Formula (0–100) |
| --- | --- | --- |
| Few papers | 30% | `100 × (1 − log(1 + papers) / log(1 + archive papers))` |
| Slow growth | 20% | `50 − 50 × log2((last 24 months + 1) / (prior 24 months + 1))`, clipped |
| No surveys / reviews | 20% | `100 / (1 + surveys / expected surveys)`; expected = papers × archive survey rate |
| Little cross-field work | 15% | `100 × (1 − share cross-listed in 2+ CS categories)` |
| Few research groups | 15% | `100 × share of papers that include one of the 5 most frequent author names` |

Verdicts: ≥ 62 under-researched (“growing” if output rose ≥ 30%), ≥ 45 developing, otherwise well developed. Confidence is low below 30 papers, medium below 300, high above.

### Score 2: underrepresented (whose work is visible)

Average of country concentration and institution concentration, each `100 × (1 − normalised Shannon entropy)` over every documented institution/country on the matching papers. It is reported only when at least 10 papers have documented affiliations; otherwise it shows “Not enough documented affiliations”. Run the paper scan above to fill them. Verdicts: ≥ 60 underrepresented, ≥ 40 somewhat concentrated, otherwise broadly represented.

### Check an idea

`GET /api/gaps/idea?q=camera based 3d printer security&category=cs.CR` scores the set of papers whose title/abstract contain **all** the keywords, with the same formulas, and adds its percentile among CS topics, the topics it lives in, and the newest matching papers. `GET /api/gaps/topics` returns every CS topic ranked.

### Limitations

- Counts reflect the imported sample until the full arXiv import finishes, and arXiv is not all of CS research (journals and conferences outside arXiv are missing).
- Keyword matching misses synonyms; try a few phrasings of an idea.
- Author names are opaque labels for the concentration signal only; nothing is inferred about the people.
- OpenAlex and PDF-text affiliations are unreviewed until an admin confirms them.
- These are signals to guide where to look, not measures of quality or importance.

## Configure the AI provider

AI calls use an OpenAI-compatible API. Set these values in the private local
`.env` file (never commit API keys or paste them into chat):

```dotenv
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=your-private-key
AI_CHAT_MODEL=gpt-4o-mini
AI_EMBEDDING_MODEL=text-embedding-3-small
```

The base URL must use HTTPS unless it points to a local model server. Local
servers on `localhost` or `127.0.0.1` may omit the API key. The selected models
must support chat completions with JSON output and the OpenAI embeddings API.
AI calls incur whatever usage charges your provider applies. No paper content
is sent to the provider until an administrator explicitly requests analysis.

Open **Admin review** in the site, enter the local `ADMIN_API_TOKEN`, and use
the review dashboard to:

- Embed and cluster the next 20 papers in the archive.
- Generate a paper summary, contribution, application, topical methods, and
  relevance explanation; review and edit these before publication.
- Record an institution or country only with a source URL and an evidence
  excerpt documented in the paper or another cited source.
- Request affiliation suggestions from the arXiv PDF's first two pages. Suggestions
  are accepted only when the quoted evidence appears verbatim in the extracted
  page text, and still require reviewer confirmation before saving.
- Approve or reject a paper. Only approved papers appear in **Featured papers**.

Admin routes require `Authorization: Bearer <ADMIN_API_TOKEN>`. The frontend
keeps the entered token in page memory only; it is not stored in source code or
browser storage.

## Deploy online

The `Dockerfile` builds the website and serves it from the same FastAPI app, so
one web service hosts everything (the site at `/`, the API at `/api/...`).

### Option A: Render (recommended, uses `render.yaml`)

1. Push this repo to GitHub (already done if you cloned it from there).
2. Sign in at [render.com](https://render.com) with GitHub, then
   **New → Blueprint** and pick this repository. Click **Apply**.
   Render creates the web service `research-discovery-network` and the
   PostgreSQL database `research-discovery-db`.
3. While it builds, fill in the optional variables it asks for
   (`AI_BASE_URL`, `AI_API_KEY`, `AI_CHAT_MODEL`, `AI_EMBEDDING_MODEL`,
   `OPENALEX_API_KEY`). Leave them blank to skip the AI features for now.
4. Load your papers into the hosted database. In Render, open
   **research-discovery-db → Connect → External Database URL** and copy it.
   Then, on your own computer in the project folder:

   ```bash
   DATABASE_URL="<External Database URL>" .venv/bin/python -m backend.migrate_to_postgres
   ```

   This copies `backend/data/papers.sqlite3` (set `PAPERS_DB` to use another
   file). Run it again any time you want to push newly imported papers.
5. Open the `https://research-discovery-network.onrender.com` URL Render
   shows. To use Admin review, copy `ADMIN_API_TOKEN` from the web service's
   **Environment** tab.

Free-plan limits: the web service sleeps after 15 minutes idle (the first
visit after that takes about a minute), and free Render PostgreSQL databases
expire after 30 days. Change the database plan to `basic-256mb`, or point
`DATABASE_URL` at a free [Neon](https://neon.tech) database, to keep the data.

### Keeping the site up to date (CI/CD)

Two GitHub Actions workflows run automatically once the repo is on GitHub:

| Workflow | When | What it does |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | every push and pull request | type-checks and builds the site, runs the Python tests, builds the Docker image and checks that it starts |
| `Update papers` (`.github/workflows/update-papers.yml`) | every day at 06:30 UTC, or by hand from the Actions tab | imports Computer Science papers arXiv added since the last run, then looks up their affiliations on OpenAlex |

- **Code changes (CD):** `render.yaml` sets `autoDeployTrigger: checksPass`, so Render
  deploys a push to `main` only after CI passes. A broken commit never goes live.
- **New papers:** the update workflow writes straight into the hosted database, so the
  live site shows them without a redeploy. One-time setup: in GitHub open
  **Settings → Secrets and variables → Actions → New repository secret** and add
  `DATABASE_URL` (the Render External Database URL). `OPENALEX_API_KEY` and
  `OPENALEX_EMAIL` are optional. Or from a terminal: `gh secret set DATABASE_URL`.
  If you change the database password, update this secret too.
- **Catch up after a gap:** Actions → Update papers → Run workflow, and enter a start date.
- **Locally:** `python -m backend.update_papers` (add `--since 2026-10-01` to choose the start date).

### Publishing: auto or review

The search, the 3D map and the gap scores always use every paper. Approval only
controls the homepage **Featured papers** list:

- `PUBLISH_MODE=auto` (set in `render.yaml`): new papers are public right away.
  Featured shows editor picks first, then the newest papers. Rejecting a paper removes
  it from Featured, and "Feature on homepage" in the Admin dashboard pins it to the top.
- `PUBLISH_MODE=review` (the default when unset, as in the URD spec): Featured shows only
  papers a reviewer approved.

### Research scope

Every arXiv CS category is included by default. To narrow it, set `RESEARCH_SCOPE=mvp`
(AI, ML, NLP and closely related areas) or a list such as `RESEARCH_SCOPE=cs.AI,cs.LG,cs.CL`.
New imports then skip papers outside the scope.

### The research assistant online

Ollama only runs on your own machine, so the hosted site can't reach it. Set
`AI_BASE_URL`/`AI_API_KEY`/`AI_CHAT_MODEL` to any hosted OpenAI-compatible
provider (OpenAI, Groq, Together, OpenRouter, ...). Without one, the
assistant still answers with cited search results, just without an LLM summary.

### Using your own model instead of OpenAI or Groq

The assistant talks to any OpenAI-compatible server, so you can run an open model you
control. Point `AI_BASE_URL` at it and set `AI_CHAT_MODEL`.

- **Run an open model (no training):** Llama, Qwen or Mistral through Ollama, vLLM or
  llama.cpp. It knows the papers through retrieval (each answer is built from the papers
  the search finds), so it doesn't need training to cover new papers.
- **Fine-tune one (optional):** LoRA fine-tuning of a small model (about 1–8B
  parameters) on approved summaries can teach it your summary style. That's possible on
  one rented or free notebook GPU. Training a model from scratch is not realistic here.
- **Hosting it:** a model needs a GPU or a large-memory machine, so it can't run on
  Render's free plan. Options are your own computer (it must stay on and be reachable
  over HTTPS, for example through a Cloudflare Tunnel), a GPU host such as Hugging Face
  Inference Endpoints, Modal or RunPod, or batch jobs that write summaries into the
  database ahead of time so the live site doesn't need the model at all.

### Option B: other hosts

- **Railway / Fly.io / any Docker host:** deploy the same `Dockerfile`, add a
  PostgreSQL database, and set `DATABASE_URL` and `ADMIN_API_TOKEN`. The
  container listens on `$PORT` (default 8000).
- **Run the image yourself:**
  `docker build -t rdn . && docker run -p 8000:8000 --env-file .env rdn`
- **Split frontend/backend (e.g. GitHub Pages + an API host):** build the site
  with `VITE_API_URL=https://your-api.example.com pnpm build`, and set
  `CORS_ORIGINS=https://you.github.io` on the API.

## Data and API

PostgreSQL stores the source/derived paper data, all CS category memberships,
review status, and resumable import state. The paper record includes title,
abstract, authors, arXiv ID/URL, publication date, categories, and reviewed
institution/country fields. Original arXiv metadata remains separate from AI
analysis, embeddings, clusters, summaries, and affiliation evidence.

The resumable arXiv OAI-PMH importer covers the Computer Science archive and
respects arXiv's metadata-harvest rate limit. It persists its cursor, so it can
be paused and resumed. If the imported SQLite database already exists, migrate
it to PostgreSQL before resuming the import.

Public endpoints:

- `GET /api/topics` — all 40 arXiv CS categories and counts.
- `GET /api/topic-links` — strongest category links, weighted by the number of
  papers cross-listed in both topics. Each topic shows up to four of its
  strongest links; line thickness and brightness reflect shared-paper counts.
- `GET /api/filters` — documented institutions, countries, and years.
- `GET /api/papers` — search and filter the imported archive with `q`,
  `category`, `country`, `institution`, `year`, `sort`, `limit`, and `offset`.
- `GET /api/featured` — approved papers only; supports the same discovery
  filters.
- `GET /api/ingest/progress` — full-import state.

Protected reviewer endpoints:

- `GET /api/admin/overview` and `GET /api/admin/reviews`.
- `POST /api/ingest`, `POST /api/ingest/full`, and `POST /api/ingest/pause` —
  import sample/full archive and pause/resume.
- `PUT /api/admin/papers/{arxiv_id}/review` — edit generated text and approval.
- `PUT /api/admin/papers/{arxiv_id}/affiliation` — save sourced affiliation
  evidence.
- `POST /api/admin/papers/{arxiv_id}/extract-affiliation` — suggest PDF-documented
  affiliation metadata with a source-checked evidence quote for human review.
- `POST /api/admin/papers/{arxiv_id}/summarize` — request an LLM analysis.
- `POST /api/admin/analyze` — generate embeddings and deterministic K-Means
  clusters for a batch.

## Ranking and limitations

The representation score is a transparent weighted average of the rarity
components that have documented data:

```text
topic rarity          = 100 × (1 − papers in primary topic / category assignments)
institution visibility = 100 × (1 − papers at institution / imported papers)
geographic visibility  = 100 × (1 − papers in country / imported papers)
score                  = available weighted average (topic 60%, institution 25%, geography 15%)
```

Institution and geographic components remain unavailable unless their
affiliation data is documented and reviewed. Names are never used to infer
identity, affiliation, country, or demographics. Similar topic frequency is
not evidence of research quality or intent.

arXiv's OAI feed generally does not supply affiliations. The dashboard accepts
only explicit source URL and evidence text for affiliation enrichment. Paper
PDF extraction proposes candidates from the first two pages and verifies that
the quoted evidence and institution name occur in the extracted text; the
reviewer must still confirm and save each candidate. Institution names are
normalized for Unicode compatibility and whitespace and matched
case-insensitively; semantically equivalent abbreviations are not guessed or
automatically merged. The app does not infer author identity, affiliations,
countries, demographics, or research intent. AI-generated text is a draft and
requires human review. Embeddings and clusters cover only papers analyzed so
far, not the entire archive; K-Means cluster IDs are technical group labels,
not validated research taxonomies.

Run backend checks with:

```sh
python -m unittest discover -s tests
```
