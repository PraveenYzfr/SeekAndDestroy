#!/usr/bin/env bash
#
# Deploy CODE and MIGRATIONS to production, without touching the data.
# Run ON THE VM.
#
#   bash scripts/deploy-app.sh
#
# WHY THIS EXISTS SEPARATELY FROM deploy-prod.sh
# ----------------------------------------------
# deploy-prod.sh is a REBUILD OF THE ESTATE: it runs reset.sql, which drops every
# table in [sad], regenerates a 74.6 MB seed and reloads 1,200 applications,
# 10,000 incidents and 54,555 configuration items. That is the right script when
# the seed itself changed.
#
# It is the wrong script for shipping code. Most deploys change Python, a
# dashboard, or add one table - and running the full one to get those costs the
# live estate, every investigation ever recorded, and twenty minutes.
#
# Having only the destructive option available is how the destructive option
# becomes the routine one. This is the routine one.
#
#   THIS SCRIPT NEVER DROPS OR RESEEDS ANYTHING.
#   No reset.sql, no schema.sql, no seed.sql. Migrations only, and every
#   migration in this repository is guarded by IF OBJECT_ID(...) IS NULL or
#   equivalent, so applying them all is safe on an already-current database.
#
# WHAT IT DOES NOT DO
# -------------------
# It does not re-embed. Vectors are keyed to row ids that this script does not
# change, so the index stays valid. If a migration you are shipping DOES change
# indexed content, run the indexer afterwards - deliberately not automatic,
# because embedding costs money and should be a decision.

set -euo pipefail
set +x                      # never echo the SA password

REPO="${REPO:-$HOME/SeekAndDestroy}"
cd "$REPO"
set -a; . "$HOME/infra/.env"; set +a

SQL='/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$P" -C -I -b'
run() { sudo docker exec -e P="$MSSQL_SA_PASSWORD" hub-sqlserver bash -c "$SQL $*"; }

echo "==> 1. pull"
git pull --ff-only
echo "    now at $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

echo "==> 2. stage migrations where the database container can read them"
sudo docker exec hub-sqlserver mkdir -p /database
sudo docker cp database/. hub-sqlserver:/database/ >/dev/null

echo "==> 3. apply migrations (idempotent - a current database is a no-op)"
# Applied in glob order, which is why they are numbered. Not skipped when
# "already applied": there is no migration-history table in this platform, and
# inventing one here would disagree with deploy-prod.sh about what has run.
# Idempotency is the mechanism instead, and it is checked - see the second pass
# in deploy-prod.sh, which every migration already survives.
for f in database/migration_0*.sql; do
    b=$(basename "$f")
    run -d SeekandDestroy -i "/database/$b" >/dev/null && echo "    $b ok"
done

echo "==> 4. build and restart the application containers"
# --no-deps so restarting ai-service does not take sqlserver, qdrant or redis
# with it. Those hold state this script is explicitly not touching.
( cd "$REPO/docker" \
  && sudo docker compose build ai-service ai-indexer api-gateway ui \
  && sudo docker run --rm --network hub docker-ui:latest nginx -t \
  && sudo docker compose up -d --no-deps ai-service ai-indexer api-gateway ui )

echo "==> 5. observability, if it is running"
# `up -d` rather than `restart`: a changed compose file - a new mount, say -
# needs the container RECREATED. `restart` reuses the old definition and the
# change silently does not land, which looks exactly like a config that does not
# work.
if sudo docker ps --format '{{.Names}}' | grep -q prometheus; then
    ( cd "$REPO/docker" && sudo docker compose --profile observability up -d prometheus grafana )
    echo "    prometheus + grafana recreated with current config"
else
    echo "    not running - skipped (start with --profile observability)"
fi

echo "==> 6. VERIFY BY QUERY - an exit code is not evidence"
# The estate counts are here to prove this script did NOT reseed. If they read
# zero, something ran a reset that should not have.
run -d SeekandDestroy -h -1 -W -Q "SET NOCOUNT ON;
SELECT CONCAT('    apps=',      (SELECT COUNT(*) FROM sad.CmdbApplication),
              ' incidents=',    (SELECT COUNT(*) FROM sad.Incident),
              ' cis=',          (SELECT COUNT(*) FROM sad.ConfigurationItem),
              ' investigations=',(SELECT COUNT(*) FROM sad.Investigation));
SELECT CONCAT('    credentials=',(SELECT COUNT(*) FROM sad.Employee WHERE PasswordHash IS NOT NULL));
SELECT CONCAT('    answer evaluations=', (SELECT COUNT(*) FROM sad.AnswerEvaluation));"

echo
echo "    Estate counts should match what was there BEFORE this ran."
echo "    If apps/incidents/cis read 0, a reset ran and you want deploy-prod.sh's"
echo "    credential restore - stop and check before logging in."
echo
echo "    Health:  curl -fsS https://sad.praveenyzfr.com/health"
echo "    Metrics: sudo docker exec docker-ai-service-1 curl -fsS localhost:8088/metrics | grep sad_"
