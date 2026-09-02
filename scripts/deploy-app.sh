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

# ad-hoc SQL, passed on STDIN rather than as an argument.
#
# run() interpolates its arguments into a string that the container's shell then
# re-parses, which is fine for `-i /database/file.sql` and breaks the moment the
# SQL contains a parenthesis:
#
#     bash: -c: line 2: syntax error near unexpected token '('
#
# That is exactly what step 6 did on two consecutive production deploys. The
# verification died, `run` was the last command in the step, and the script
# carried on and reported success - having verified nothing. A script whose own
# header argues that an exit code is not evidence spent two deploys proving it.
#
# stdin has no quoting layer to get wrong, so this cannot recur by adding a
# parenthesis.
runq() {
    sudo docker exec -i -e P="$MSSQL_SA_PASSWORD" hub-sqlserver \
        bash -c '/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$P" -C -I -b -d SeekandDestroy -h -1 -W -i /dev/stdin'
}

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

echo "==> 3b. GRANTS - a created table is not a writable one"
# Added after migrations 020 and 021 shipped, both tables were created, and both
# were UNWRITABLE. The service logs a warning and swallows the failure by design,
# because grading must never break a delivered answer - so the platform computed
# verdicts, stored none, and reported itself healthy.
#
# db-init.sh in this repository grants SELECT on the whole sad schema and INSERT
# PER TABLE, so every new writable table needs an explicit line. This script had
# no grant step at all, which meant it created tables and never made them
# writable, silently, for every migration.
#
# THE FILE THIS RUNS IS NOT docker/db-init.sh. Production executes
# ~/infra/provision-databases.sh via the db-provision service, and the two have
# DIVERGED - grants added to one are absent from the other, and which grants a
# table gets has depended on which session edited which file. deploy-prod.sh
# already calls db-provision, so this calls the same thing rather than inventing
# a third path.
#
# The divergence itself is not fixable from this repository: provision-databases.sh
# lives on the VM and is not version controlled here. Naming it is the most this
# script can do about it.
( cd "$HOME/infra" && sudo docker compose up db-provision ) || {
    echo "    WARNING: db-provision failed. Tables created by the migrations above"
    echo "    may exist and be UNWRITABLE, and the service will not report it -"
    echo "    it logs a warning and continues. Verify with the write test below."
}

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
# The estate counts prove this script did NOT reseed. If they read zero,
# something ran a reset that should not have.
runq <<'VERIFY'
SET NOCOUNT ON;
SELECT CONCAT('    apps=',       (SELECT COUNT(*) FROM sad.CmdbApplication),
              ' clusters=',      (SELECT COUNT(*) FROM sad.InfrastructureCluster),
              ' incidents=',     (SELECT COUNT(*) FROM sad.Incident),
              ' cis=',           (SELECT COUNT(*) FROM sad.ConfigurationItem));
SELECT CONCAT('    credentials=',(SELECT COUNT(*) FROM sad.Employee WHERE PasswordHash IS NOT NULL));
VERIFY

echo "==> 7. WRITE TEST - existence is not writability"
# The check that caught migrations 020 and 021 shipping unwritable. A table can
# be created, present, queryable and still reject every INSERT, and the one
# component that would notice swallows the error on purpose.
#
# Rolled back, so this proves the permission without leaving a row behind.
runq <<'WRITETEST' || echo "    WRITE TEST FAILED - see the grant note in step 3b"
SET NOCOUNT ON;
BEGIN TRAN;
BEGIN TRY
    -- Column lists and values are taken from the migrations, not from memory.
    -- Site and Source are NOT NULL with no default, and Source is CHECK-
    -- constrained to ('python','judge') - an INSERT missing either fails on the
    -- constraint rather than on the permission, which would report a grant
    -- problem that does not exist and hide one that does.
    INSERT INTO sad.EvalRun (Suite, Status)
        VALUES ('_writetest', 'Running');
    INSERT INTO sad.RemediationTask (Site, Source, Status)
        VALUES ('_writetest', 'python', 'Queued');
    PRINT '    writable: EvalRun, RemediationTask';
END TRY
BEGIN CATCH
    PRINT CONCAT('    NOT WRITABLE: ', ERROR_MESSAGE());
END CATCH;
ROLLBACK;
WRITETEST

