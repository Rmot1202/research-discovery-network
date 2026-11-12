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
uvicorn backend.main:app --port 8001 --reload
```

The backend loads `.env.local` (the Neon strings from `neon env pull --file .env.local`) and then
`.env` by itself; variables already set in your shell win. Don't add `--env-file .env`: it would
load `.env` first and override `.env.local`. To work against local Docker PostgreSQL instead,
see [Start locally with PostgreSQL](#start-locally-with-postgresql). Setup history, operations
and troubleshooting are in [docs/OPERATIONS.md](docs/OPERATIONS.md).

Terminal 2, the website:

```sh
npx pnpm install   # first time, or after package.json changes
npx pnpm dev       # opens on http://localhost:8443
```

Run the tests with `python -m unittest discover -s tests`. They always use a throwaway SQLite
file and never read `.env`, `.env.local` or your shell's `DATABASE_URL`, so they can't touch Neon.

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
.venv/bin/python -m backend.affiliation_scan --sources openalex --limit 2000 # OpenAlex only
.venv/bin/python -m backend.affiliation_scan --sources openalex+pdf --limit 2000 # OpenAlex, then PDF fallback
.venv/bin/python -m backend.affiliation_scan --limit 2000      # PDF only (default for CLI)
.venv/bin/python -m backend.affiliation_scan                   # whole archive (resumable; Ctrl+C pauses)
.venv/bin/python -m backend.affiliation_scan --sources openalex+pdf --limit 300 # also read PDFs for OpenAlex misses
.venv/bin/python -m backend.affiliation_scan --status          # progress
.venv/bin/python -m backend.affiliation_scan --restart         # start a new pass from the first paper
```

The admin dashboard always checks OpenAlex first; its **Also read PDFs** option enables the slower PDF fallback.

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

### Score 2: underrepresented (who the work in the collection comes from)

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

Admin routes accept either a Firebase ID token from **Sign in with Google** (the email must be
in `ADMIN_EMAILS`, or the user must have the `admin` custom claim) or
`Authorization: Bearer <ADMIN_API_TOKEN>` for scripts. The token is kept in page memory only.

## Deploy online

The `Dockerfile` builds the website and serves it from the same FastAPI app, so
one web service hosts everything (the site at `/`, the API at `/api/...`).

### Option A: Render + Neon + Firebase (recommended, uses `render.yaml`)

The pieces, all on free plans:

| Piece | Service | Why |
|---|---|---|
| Website + API | Render web service | builds the `Dockerfile` |
| Database | [Neon](https://neon.com) PostgreSQL | Render's free PostgreSQL is deleted after 30 days (plus a 14-day grace period); Neon's free plan does not expire |
| Admin sign-in | Firebase Authentication (Google) | no passwords to manage; only emails in `ADMIN_EMAILS` get admin access |
| Daily imports | GitHub Actions (`update-papers.yml`) | nothing to run on your own computer |

1. **Database.** This repo is linked to Neon project `shy-cell-28779860`, branch `production`
   (`neon.ts` + the gitignored `.neon`). To get the connection strings locally:

   ```bash
   npm i -g neon@latest
   neon login                       # one-time browser sign-in
   neon env pull --file .env.local  # writes DATABASE_URL (pooled) and DATABASE_URL_UNPOOLED (direct)
   ```

   The backend reads env in this order: real environment variables, then `.env.local`, then `.env`
   (a file never overrides a variable that is already set; `RDN_SKIP_ENV_FILES=1` turns file loading
   off, and the tests always do). Use the **pooled** `DATABASE_URL` (`-pooler` host) for the app,
   Render and the GitHub Action; `backend.migrate_to_postgres` and `pg_dump`/`pg_restore` use the
   **direct** `DATABASE_URL_UNPOOLED`. Never commit `.env.local` (it is in `.gitignore`).
   Moving from an existing Render database (done on 2026-11-11, see
   [docs/OPERATIONS.md](docs/OPERATIONS.md#move-data-between-databases)):
   `RENDER_URL="<Render External URL>" NEON_URL="<DATABASE_URL_UNPOOLED>" bash scripts/move_db_to_neon.sh`.
   It needs `pg_dump`/`pg_restore` 16+; on an Intel Mac, run it inside Docker instead of building
   `libpq` with Homebrew. Starting fresh: leave it empty; the daily job fills it.
2. **Firebase.** In the [Firebase console](https://console.firebase.google.com) create a project,
   then **Build → Authentication → Get started → Sign-in method → Google → Enable**. Under
   **Authentication → Settings → Authorized domains** add `research-discovery-network.onrender.com`.
   Under **Project settings → General → Your apps** add a Web app and note `apiKey`,
   `authDomain` and `projectId`.
3. **Render.** **New → Blueprint**, pick this repository, **Apply**, and fill in:
   `DATABASE_URL` (Neon pooled string), `FIREBASE_PROJECT_ID`, `FIREBASE_WEB_API_KEY`,
   `FIREBASE_AUTH_DOMAIN`, `ADMIN_EMAILS` (comma-separated, for example `you@gmail.com`).
   `ADMIN_API_TOKEN` is generated and still works as a fallback login for scripts.
   If you already have the old Blueprint with `research-discovery-db`, delete that database in
   Render after the Neon copy is checked.
4. **GitHub secrets** (Settings → Secrets and variables → Actions): `DATABASE_URL` (the same pooled Neon
   string) and `OPENALEX_API_KEY` (free at [openalex.org/settings/api](https://openalex.org/settings/api)).
   Then Actions → Update papers → **Run workflow** once to test.
5. Open the site → Admin → **Sign in with Google**.

Free-plan limits: the Render web service sleeps after 15 minutes idle (the first visit after that
takes about a minute). Neon's free plan has 1 GB of storage per project; the importer stops adding
papers at `DB_STORAGE_LIMIT_MB` (default 900) so the database never fills up.

### Keeping the site up to date (CI/CD)

| Workflow | When | What it does |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | every push and pull request | type-checks and builds the site, runs the Python tests, builds the Docker image and checks that it starts |
| `Update papers` (`.github/workflows/update-papers.yml`) | every day at 06:30 UTC, or by hand from the Actions tab | arXiv import + backfill, OpenAlex import, affiliations, embeddings, summaries |

- **Code changes (CD):** `render.yaml` sets `autoDeployTrigger: checksPass`, so Render
  deploys a push to `main` only after CI passes. If CI is disabled or never reports, Render does
  not deploy at all; use Render → **Manual Deploy → Deploy latest commit**.
- **How to change code:** work on a branch, open a pull request, wait for CI, then merge.
  Merging to `main` is what deploys the site.
- **Actions minutes:** the repo is public, so GitHub Actions minutes are free and unlimited. If it
  ever goes private again, the daily job (about 80-100 minutes with the 700-paper PDF scan) uses
  most of the 2,000 free minutes a month; lower `SUMMARY_MINUTES` and `OPENALEX_MINUTES` in the
  repo's Actions variables.
- **No more local loading:** the workflow writes straight into Neon. `migrate_to_postgres`
  is only needed once if you want to copy an old local SQLite file.
- **Catch up after a gap:** Actions → Update papers → Run workflow, and enter a start date.

### Daily imports: arXiv and OpenAlex

Each run does two lanes per source:

- **arXiv:** papers added since the last run, then `ARXIV_BACKFILL_PAGES` (default 10, about
  10,000 records) of older weeks, walking back toward 2005.
- **OpenAlex** (`backend/openalex_import.py`): Computer Science works (field 17) with an abstract,
  English, not retracted, articles/preprints/reviews. First the last `OPENALEX_RECENT_DAYS` (7) of
  publications, then one older publication day at a time back to `OPENALEX_START_DATE`
  (2015-01-01), remembering where it stopped. Works that are also on arXiv are skipped (arXiv
  already has them). Authors' institutions and countries come with each work, so these papers
  need no affiliation scan.
- **Budget:** a free OpenAlex key has $1/day (resets at midnight UTC); one list call costs
  $0.0001 and returns up to 100 works, so the money allows far more than the job can store. The
  importer reads the remaining budget from the response headers and keeps `OPENALEX_RESERVE_CREDITS`
  (1,000 calls) for the affiliation step. The real limits are `OPENALEX_DAILY_MAX` (20,000 new
  papers), `OPENALEX_MINUTES` (20) and the database size.
- **Affiliations:** the daily workflow checks OpenAlex first for up to 5,000 recent arXiv papers,
  then checks up to `AFFILIATION_DAILY_LIMIT` PDFs (default 700, about 35 minutes at one every
  three seconds). The workflow allows up to 120 minutes so affiliation lookups have room to finish
  before embeddings and summaries run.
- OpenAlex IDs (`W...`) are stored in the same `arxiv_id` column, link to `openalex.org/works/W...`,
  and the assistant cites them as `[OpenAlex:W...]`.
- Test locally: `python -m backend.openalex_import --dry-run --max-new 50`.

### Admin dashboard: today's papers

The dashboard opens on **papers loaded today** (by `ingested_at`, in your time zone), with counts
per source, the database size and the import run log. Use the date picker to look at another day
and the source filter for arXiv or OpenAlex only. Older papers loaded before this change have no
`ingested_at`, so they only show under "all days" in search, not in the daily view.

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
control: point `AI_BASE_URL` at it and set `AI_CHAT_MODEL`. `training/` has a full kit to
fine-tune a small open model (Qwen2.5 1.5B/3B) for this exact job and serve it with Ollama;
see [training/README.md](training/README.md).

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

### Discovery Score (paper level, MVP)

Each paper gets a 0–100 **Discovery Score** built from three separate factors.
Every factor compares the paper with other papers in the **same field** (its
primary arXiv category, for example `cs.CV`):

| Factor | Default weight | What is counted inside the field |
| --- | --- | --- |
| Topic underrepresentation | 40% | papers in the paper's subtopic vs. the field's other subtopics (from `analysis_pipeline cluster`; until a field is clustered, the field itself vs. the other CS categories) |
| Institutional underrepresentation | 30% | papers from the paper's documented institution vs. other institutions in the field |
| Geographic underrepresentation | 30% | papers from the paper's documented country vs. other countries in the field |

Each factor is normalized the same way, on a log scale within the field:

```text
factor = 100 × (ln(largest group) − ln(this group)) / (ln(largest group) − ln(smallest group))
```

100 = the least common group in the field, 0 = the most common (all groups the
same size → 0). The log scale stops one huge group from pushing everyone else to
the top. A factor is left out when the value is not documented or the field has
fewer than 20 documented papers (`DISCOVERY_MIN_FIELD_SAMPLE`).

```text
Discovery Score = Σ(weight × factor) / Σ(weights of the factors that are available)
```

`coverage` reports how much of the full weight was available (1.0 = all three).
For sorting only, a missing factor counts as a neutral 50, so a paper is not
pushed to the top just because its institution or country is unknown (the
displayed score is unchanged).

**One strong factor is enough.** A paper is flagged as underrepresented when
the Discovery Score is ≥ 60 (`DISCOVERY_OVERALL_THRESHOLD`) **or** any single
factor is ≥ 75 (`DISCOVERY_FACTOR_THRESHOLD`). So a rare topic from a large
institution, or a common topic from a rarely seen country, still shows up, with
the reason ("Underrepresented topic", "Underrepresented country or region", …).

**Representation, not visibility.** The score only measures how often something
appears in the imported arXiv dataset. It says nothing about whether the work is
getting attention; citations, downloads and other visibility signals are planned
for a later version.

**Experimenting with weights.** Set `DISCOVERY_WEIGHTS="topic=0.5,institution=0.25,geography=0.25"`
(any numbers; they are rescaled to sum to 1), or pass `?weights=topic=50,institution=25,geography=25`
to any endpoint below.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/papers?sort=score` | papers ranked by Discovery Score (the site's "Sort by → Discovery Score") |
| `GET /api/discovery?category=cs.CV&factor=geography&min_factors=2` | ranked list; `factor` keeps papers flagged on that factor, `min_factors` keeps papers with at least that many measurable factors |
| `GET /api/discovery/fields/cs.CV` | least represented subtopics, institutions and countries in one field |

Every paper in the API carries `discovery_score` and a `discovery` object with
`factors`, `weights`, `coverage`, `flags`, `reasons` and `topic_basis`. The code is
in `backend/discovery_score.py`; tests in `tests/test_discovery_score.py`.

Institution and geography factors stay empty until affiliation data is
documented (PDF scan or manual entry). Names are never used to infer
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
