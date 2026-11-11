#!/usr/bin/env bash
# One-time move of the paper database from Render Postgres (expires) to Neon (free, no expiry).
#
#   1. Create a free Neon project at https://neon.com (pick a US East region near Render's).
#   2. Copy its connection string (Dashboard -> Connect -> "Connection string").
#   3. Copy the Render database's "External Database URL".
#   4. Run:
#        RENDER_URL='postgresql://...render.com/research_discovery' \
#        NEON_URL='postgresql://...neon.tech/neondb?sslmode=require' \
#        bash scripts/move_db_to_neon.sh
#   5. Put NEON_URL in Render's DATABASE_URL and in the GitHub secret DATABASE_URL.
#
# Needs pg_dump/pg_restore 16+ (macOS: brew install libpq && brew link --force libpq).
set -euo pipefail
: "${RENDER_URL:?Set RENDER_URL to the Render External Database URL}"
: "${NEON_URL:?Set NEON_URL to the Neon connection string}"

dump="research_discovery_$(date +%Y%m%d_%H%M).dump"
echo "Dumping Render database to $dump ..."
pg_dump "$RENDER_URL" --format=custom --no-owner --no-privileges --file "$dump"
echo "Restoring into Neon ..."
pg_restore --dbname "$NEON_URL" --no-owner --no-privileges --clean --if-exists "$dump"
echo "Row counts on Neon:"
psql "$NEON_URL" -c "SELECT COUNT(*) AS papers FROM papers;" \
                 -c "SELECT pg_size_pretty(pg_database_size(current_database())) AS size;"
echo "Done. Keep $dump as a backup; Render Free has no backups of its own."
