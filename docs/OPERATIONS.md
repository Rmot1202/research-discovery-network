# Operations guide

How the live Research Discovery Network is set up, how to run it day to day, and fixes for
problems that have already come up. Setup steps for a new deployment are in the
[README](../README.md#deploy-online).

Never paste connection strings, tokens or `.env*` files into issues, chats or commits. This
document lists where secrets live, never their values.

## Current setup (as of 2026-11-11)

| Piece | Where | Notes |
|---|---|---|
| Website + API | Render web service `research-discovery-network`, https://research-discovery-network.onrender.com | Docker build from `Dockerfile`; deploys from `main` after CI passes (`autoDeployTrigger: checksPass`) |
| Database | Neon project `shy-cell-28779860`, branch `production` (`br-falling-night-bbig8w57`), database `neondb`, region us-east-1 | 68,624 papers / 313 MB after the move from Render; the importer stops at 900 MB (Neon Free has 1 GB) |
| Old database | Render Postgres (free) | Not used any more. Delete it after a few days of the site running on Neon. A backup dump is in `~/rdn-backups/` on Raven's Mac |
| Daily imports | GitHub Actions `Update papers`, 06:30 UTC | Writes straight to Neon. Repo is public, so Actions minutes are free |
| CI | GitHub Actions `CI` | Type-check + build, Python tests, Docker image check; required for Render to deploy |
| Admin sign-in | Firebase Google sign-in (emails in `ADMIN_EMAILS`) or `ADMIN_API_TOKEN` | Firebase variables set on Render 2026-11-11 |
| Agent tooling | `.agents/skills` (Neon skills, pinned in `skills-lock.json`), `.mcp.json`, `.codex/config.toml`, `.vscode/mcp.json` | Neon MCP server `https://mcp.neon.tech/mcp`, OAuth sign-in on first use, no keys in the repo |

### Where each setting lives

| Name | Render (web service → Environment) | GitHub (Settings → Secrets and variables → Actions) | Local Mac |
|---|---|---|---|
| `DATABASE_URL` (Neon **pooled**, `-pooler` host) | yes | secret | `.env.local` |
| `DATABASE_URL_UNPOOLED` (Neon **direct**) | no | no | `.env.local` (migrations, `pg_dump`/`pg_restore`) |
| `ADMIN_API_TOKEN` | yes | no | optional in `.env` |
| `OPENALEX_API_KEY` | optional (only for the admin "scan affiliations" button) | secret: turns on the daily OpenAlex import and OpenAlex affiliations | optional in `.env` |
| `FIREBASE_PROJECT_ID`, `FIREBASE_WEB_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `ADMIN_EMAILS` | when Firebase is set up | no | no |
| `PUBLISH_MODE` | `auto` | no | `.env` |

Load order in the backend (`backend/env_files.py`): real environment variables, then `.env.local`,
then `.env`. A file never overrides a variable that is already set. `RDN_SKIP_ENV_FILES=1` turns file
loading off (the tests always set it).

## Day to day

### Run locally against Neon

```sh
source .venv/bin/activate
uvicorn backend.main:app --port 8001 --reload   # terminal 1
npx pnpm dev                                    # terminal 2
```

Check which database the backend will use (prints only the host):

```sh
python -c "from backend.database import database_url; print(database_url().split('@')[-1].split('/')[0])"
```

It should end in `-pooler.….neon.tech`. If it shows `localhost` or `127.0.0.1`, something in your
shell is overriding `.env.local` (see [Troubleshooting](#troubleshooting)).

### Get or refresh the Neon connection strings

```sh
npm i -g neon@latest
neon login
neon env pull --file .env.local
```

`.env.local` is gitignored. `neon env pull` without `--file` writes into `.env` when one exists.

### Change code

1. Create a branch, commit, push, and open a pull request.
2. Wait for CI (backend, frontend, docker) to pass.
3. Merge. Render deploys `main` automatically once CI on `main` passes.
4. If Render does not deploy (CI stuck, disabled or failed), use Render → **Manual Deploy →
   Deploy latest commit**.

### Run an import by hand

GitHub → Actions → **Update papers** → **Run workflow**. Leave the date blank to import everything
since the last run, or enter a start date to catch up after a gap. Admin → "Papers loaded today"
shows the result, the database size and the run log.

### Move data between databases

`scripts/move_db_to_neon.sh` dumps one database and restores it into another. Use the **direct**
Neon string for the target. On an Intel Mac, run it in Docker so nothing has to be compiled:

```sh
read -s "RENDER_URL?Paste source database URL: "
export RENDER_URL
export NEON_URL="$(grep '^DATABASE_URL_UNPOOLED=' .env.local | cut -d= -f2- | tr -d '"')"
docker run --rm -e RENDER_URL -e NEON_URL -v "$PWD":/work -w /work postgres:18 bash scripts/move_db_to_neon.sh
unset RENDER_URL NEON_URL
mkdir -p ~/rdn-backups && mv research_discovery_*.dump ~/rdn-backups/
```

`--clean --if-exists` replaces the target's tables, so only point `NEON_URL` at a database you
mean to overwrite. Dumps are gitignored (`*.dump`); keep them outside the repo anyway.

### Set a GitHub secret without the `gh` CLI

```sh
grep '^DATABASE_URL=' .env.local | cut -d= -f2- | tr -d '"' | pbcopy
```

Paste it at Settings → Secrets and variables → Actions → New repository secret, then clear the
clipboard with `pbcopy < /dev/null`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Tests or the API try `127.0.0.1:5432` and get "Connection refused" | A `DATABASE_URL` in the shell points at local Docker PostgreSQL. VS Code's `"python.terminal.useEnvFile": true` (in `.vscode/settings.json`) loads `.env` into every new terminal | Remove `DATABASE_URL` from `.env` (Neon's is in `.env.local`), or `unset DATABASE_URL`. Tests ignore it either way |
| `uvicorn --env-file .env` uses the wrong database | `--env-file` loads `.env` into the environment before the app starts, so it beats `.env.local` | Start without `--env-file` |
| `pip install` fails building `cryptography` with "rustc 1.82.0 is not supported" (Intel Mac) | `cryptography` 49+ ships no Intel macOS wheel and needs Rust 1.83 to build | Already handled: `backend/requirements.txt` pins `cryptography<49` on Intel Macs |
| `pg_dump: command not found`, or Homebrew starts compiling `libpq` | Homebrew no longer ships bottles for Intel Macs | Use the Docker command in [Move data between databases](#move-data-between-databases) |
| `neon deploy`: "Cannot find module '@neon/config/v1'" | Node packages not installed | `npx -y pnpm@10 install` (no global `pnpm` needed) |
| A pasted command fails with "Unknown commands: #, finish, the …" | zsh does not treat `#` as a comment in interactive shells by default | Paste commands without trailing comments, or run `setopt interactivecomments` |
| Neon console shows ~71K papers, or blank counts for some tables | The sidebar shows Postgres estimates, which are blank until the table is analyzed | Run `ANALYZE;` in the SQL Editor, or trust Admin → "Papers loaded today" for exact numbers |
| Neon console shows lock icons on tables | Row-level security is off | Fine: the app connects through the backend. Keep Neon's Data API off unless you add RLS policies |
| CI runs stay "Queued" with no jobs and can't be cancelled | A GitHub-side glitch (five runs from 2026-11-11 15:18–15:45 ET are stuck this way; the API refuses to cancel or delete them) | Harmless. Push a new commit or re-run to get a fresh run. Contact GitHub Support if new runs also stay queued |
| Admin shows no "Papers loaded today" row | Render is still serving older code | Check Render → Events for the live commit; Manual Deploy if needed |
| "0 new papers" right after a database move | The move keeps each paper's original load date | Expected; the next import adds today's papers |
| `test_only_approved_papers_are_returned_in_the_public_featured_collection` fails with `2 != 1` | `PUBLISH_MODE=auto` in the shell (from `.env`) | Fixed: the test pins review mode |

## Change log

### 2026-11-11

- **Automated imports** (PR #1): daily arXiv import + backfill and OpenAlex import in GitHub
  Actions, `import_runs` log with `source` and `ingested_at`, admin "Papers loaded today" view,
  Firebase admin sign-in (code only), assistant training kit in `training/`.
- **Local backend changes** merged from the Mac (`backend/affiliation_scan.py`).
- **Neon** (PR #2): repo linked to Neon (`neon.ts`, gitignored `.neon`), `.env.local` loading,
  pooled vs direct connection handling, `pool_recycle` for Neon idle timeouts, migrations over the
  direct connection, Neon agent skills and MCP config, Intel Mac `cryptography` pin, tests isolated
  from shell and `.env*` settings.
- **Data move:** Render Postgres → Neon (68,624 papers, 313 MB). Render `DATABASE_URL` and the
  GitHub secret `DATABASE_URL` set to Neon's pooled string (secret added 16:25 ET). Repo made public.
- **Cleanup:** merged branches deleted; `.dockerignore` excludes agent tooling and docs; this guide
  added.
- **Old full import retired:** the admin dashboard's one-time "Ingest all arXiv CS papers" import
  stopped on 2026-11-04 (arXiv timed out after 53 pages). Once the daily job has run, a stopped
  full import is marked `retired` and the homepage shows the daily job's last run instead of the old
  error. arXiv requests now wait 90 s and retry up to 3 times.
- **Firebase** Google sign-in configured on Render.

## Still to do

- [ ] Confirm Render's `DATABASE_URL` is the Neon pooled string: after today's import, Admin →
      "Papers loaded today" should show new papers (an old Render database would show none).
- [ ] Delete the old Render Postgres database after a few days on Neon (backup in `~/rdn-backups/`).
- [ ] Firebase: check that Google sign-in works on the live site (Firebase → Authentication →
      Settings → Authorized domains must list `research-discovery-network.onrender.com`).
- [ ] Optional: add `OPENALEX_API_KEY` as a GitHub secret to turn on the daily OpenAlex import.
- [ ] Optional: rename the Neon project from "my-first-project" (Neon → Settings).
