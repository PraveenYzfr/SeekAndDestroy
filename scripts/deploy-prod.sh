#!/usr/bin/env bash
#
# Deploy SeekAndDestroy to production. Run ON THE VM.
#
# Every step below exists because skipping it has already broken production once.
# The comments say which, because a runbook that only says what to do gets
# shortened by the next person under time pressure.
#
#   credentials before reset   reset.sql drops sad.Employee and PasswordHash is a
#                              column on it. Praveen set E1001's password by hand;
#                              without the carry he is locked out of
#                              sad.praveenyzfr.com with no way back in.
#   migrations BEFORE seed     the seed inserts into tables migrations 007-011
#                              create. Running seed first loads the CMDB tables
#                              and silently skips 10,000 incidents - which is
#                              exactly what happened, and the seed reported
#                              success because sqlcmd exited 0 on a partial load.
#   seed generated HERE        database/seed.sql is no longer committed. It is
#                              74.6 MB and GitHub refuses past 100. The generator
#                              is deterministic and stdlib-only, so this produces
#                              byte-identical output from the same commit.
#   grants AFTER migrations    a reset drops all 21. Forgetting produced "DELETE
#                              permission was denied on IndexWatermark" in prod.
#   qdrant recreated           vectors are keyed to row ids from the OLD corpus.
#                              Leaving them mixes two estates in one index.
#   verify by QUERY            not by exit code. See the seed note above.
#
#   NOBODY RUNS TESTS DURING THIS. reset.sql drops every table in [sad] and the
#                              seed reloads them, so IDENTITY restarts at 1. A test
#                              that created an investigation and holds its id loses
#                              the parent row underneath it and fails on a foreign
#                              key into sad.AgentAuditLog - which reads as a bug in
#                              audit logging, in code that is fine. A false GREEN
#                              (a suite that skips) costs a missed signal; a false
#                              RED with a confident wrong cause costs an hour and,
#                              in CI, earns an @flaky decorator that buries it. This
#                              is not limited to CMDB tests: anything that writes a
#                              row and reads it back later is unsafe in this window.
#                              Announce the start and the end.
set -euo pipefail
set +x                      # never echo the SA password

REPO="${REPO:-$HOME/SeekAndDestroy}"
cd "$REPO"
set -a; . "$HOME/infra/.env"; set +a

SQL='/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$P" -C -I -b'
run() { sudo docker exec -e P="$MSSQL_SA_PASSWORD" hub-sqlserver bash -c "$SQL $*"; }

echo "==> 0. indexer down - it must not write during the swap"
sudo docker stop docker-ai-indexer-1 >/dev/null 2>&1 || true

echo "==> 1. generate the seed (deterministic, ~2 min)"
python3 scripts/generate_seed.py
ls -la database/seed.sql | awk '{printf "    seed.sql %.1f MB\n", $5/1048576}'

echo "==> 2. stage SQL where the database container can read it"
sudo docker exec hub-sqlserver bash -c 'rm -rf /database' 2>/dev/null || true
sudo docker cp database/. hub-sqlserver:/database/ >/dev/null
cp scripts/preserve_credentials.sql scripts/restore_credentials.sql /tmp/ 2>/dev/null || true
sudo docker cp /tmp/preserve_credentials.sql hub-sqlserver:/database/ 2>/dev/null || true
sudo docker cp /tmp/restore_credentials.sql  hub-sqlserver:/database/ 2>/dev/null || true

echo "==> 3. carry credentials OUT of the schema about to be dropped"
run -d SeekandDestroy -h -1 -W -i /database/preserve_credentials.sql

echo "==> 4. reset, schema, migrations, then seed - in that order"
run -d SeekandDestroy -i /database/reset.sql  >/dev/null && echo "    reset ok"
run -d SeekandDestroy -i /database/schema.sql >/dev/null && echo "    schema ok"
for f in database/migration_0*.sql; do
    b=$(basename "$f")
    run -d SeekandDestroy -i "/database/$b" >/dev/null && echo "    $b ok"
done
run -d SeekandDestroy -i /database/seed.sql >/dev/null && echo "    seed ok"

echo "==> 4b. migrations AGAIN, after the seed"
# Not belt-and-braces. Migrations run BEFORE the seed, so any migration that
# UPDATEs seeded rows acts on an empty table and silently does nothing - which is
# how E1001 lost the administrator grant migration 006 exists to give it, and how
# the CI backfill in 008 linked zero incidents on a clean database. Both looked
# like working migrations because neither errors on an empty table.
#
# Every migration is idempotent, so a second pass is safe. That property is now
# load-bearing rather than merely claimed, and three of them were not idempotent
# until tonight.
for f in database/migration_0*.sql; do
    b=$(basename "$f")
    run -d SeekandDestroy -i "/database/$b" >/dev/null || echo "    $b FAILED on second pass"
done
echo "    second pass complete"

echo "==> 5. restore credentials"
run -d SeekandDestroy -h -1 -W -i /database/restore_credentials.sql

echo "==> 6. re-apply grants - the reset dropped all of them"
( cd "$HOME/infra" && sudo docker compose up db-provision )

echo "==> 7. recreate the vector collection"
# GUARDED. This step is correct ONLY when the seed above replaced the corpus:
# vectors are keyed to row ids, so an old index over a new estate mixes two
# estates in one collection. It is catastrophic in every other case.
#
# Running this script for a CODE deploy drops a complete, current index and
# forces a two-hour, billed re-embed. That nearly happened tonight at 92%
# through a 96,936-document run, and what stopped it was a teammate reading
# the script - not the script. SKIP_REINDEX=1 makes the safe case expressible.
if [ "${SKIP_REINDEX:-0}" = "1" ]; then
    echo "    SKIP_REINDEX=1 - leaving the existing collection alone"
else
COLL=$(run -d SeekandDestroy -h -1 -W -Q \
  "SET NOCOUNT ON; SELECT TOP 1 CollectionName FROM sad.IndexRun WHERE CollectionName IS NOT NULL ORDER BY RunId DESC" \
  | tr -d '[:space:]')
if [ -n "${COLL:-}" ]; then
    sudo docker run --rm --network hub curlimages/curl:latest \
        -s -X DELETE "http://hub-qdrant:6333/collections/$COLL" >/dev/null
    echo "    dropped $COLL"
fi
fi

echo "==> 8. rebuild and restart the services whose code changed"
# ui IS IN THIS LIST. It was not, and nothing said so - every UI change shipped
# tonight went out by luck or not at all. nginx.conf and the built bundle are
# baked in at image build time, so a restart ships nothing.
#
# --no-deps IS LOAD-BEARING. Production runs THESE containers against the shared
# hub-* infrastructure; the sqlserver/qdrant/redis defined in this compose file
# are unused duplicates. Without --no-deps, `up ai-service` follows depends_on,
# starts the duplicate sqlserver, which has no SA password, fails the password
# policy and takes its dependents down with it. That is not hypothetical - it
# took production down tonight.
#
# nginx -t runs against the NEW image BEFORE anything restarts. A config that
# fails to parse takes the whole front end down. The compose network is needed
# for the test: proxy_pass names api-gateway, which does not resolve on a bare
# `docker run`.
( cd "$REPO/docker" \
  && sudo docker compose build ai-service ai-indexer api-gateway ui \
  && sudo docker run --rm --network hub docker-ui:latest nginx -t \
  && sudo docker compose up -d --no-deps ai-service ai-indexer api-gateway ui )

echo "==> 9. VERIFY BY QUERY - an exit code is not evidence"
run -d SeekandDestroy -h -1 -W -Q "SET NOCOUNT ON;
SELECT CONCAT('    apps=',       (SELECT COUNT(*) FROM sad.CmdbApplication),
              ' clusters=',      (SELECT COUNT(*) FROM sad.InfrastructureCluster),
              ' incidents=',     (SELECT COUNT(*) FROM sad.Incident),
              ' notes=',         (SELECT COUNT(*) FROM sad.IncidentComment),
              ' changes=',       (SELECT COUNT(*) FROM sad.Change));
SELECT CONCAT('    cis=',        (SELECT COUNT(*) FROM sad.ConfigurationItem),
              ' edges=',         (SELECT COUNT(*) FROM sad.CiRelationship),
              ' vms=',           (SELECT COUNT(*) FROM sad.CiVmInstance),
              ' servers=',       (SELECT COUNT(*) FROM sad.CiServer),
              ' volumes=',       (SELECT COUNT(*) FROM sad.CiStorageVolume));
SELECT CONCAT('    credentials=',(SELECT COUNT(*) FROM sad.Employee WHERE PasswordHash IS NOT NULL));"

cat <<'DONE'

    Expected, from the same commit:
      apps=1200 clusters=256 incidents=10000 notes~89800 changes=1000
      cis=54555 edges=85526 vms=30105 servers=10931 volumes=6000
      credentials>=1        <-- if this is 0, Praveen cannot log in. Stop and restore.

    Then start the re-embed:
      sudo docker start docker-ai-indexer-1
DONE
