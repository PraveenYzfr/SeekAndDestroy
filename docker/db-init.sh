#!/bin/bash
# One-shot database initialisation for the containerised SQL Server.
#
# Runs to completion and exits; ai-service and api-gateway wait on that exit
# via depends_on: service_completed_successfully, so nothing starts against a
# half-built database.
#
# Idempotent by guard, not by script: schema.sql creates tables
# unconditionally and seed.sql would insert all 96k rows a second time, so
# neither can simply be re-run. The guard is the presence of sad.Employee -
# if the schema is already there, this exits successfully and changes nothing.
# That makes `docker compose up` safe to run any number of times.
set -euo pipefail

SQLCMD="/opt/mssql-tools18/bin/sqlcmd"
SERVER="sqlserver"
# Overridable so this stack can share one SQL Express instance with the
# other co-hosted systems - see the multi-system hub SHARED_PLAN.md.
DB="${DB_NAME:-PraveenDB}"
# -C: trust the container's self-signed certificate. -b: exit non-zero on a
# SQL error, so `set -e` can actually stop the script.
COMMON=(-S "$SERVER" -U sa -P "$SA_PASSWORD" -C -b)

echo "==> waiting for SQL Server"
for i in $(seq 1 60); do
    if "$SQLCMD" "${COMMON[@]}" -Q "SELECT 1" >/dev/null 2>&1; then
        echo "    up after ${i}0s"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "    SQL Server never became reachable" >&2
        exit 1
    fi
    sleep 10
done

echo "==> database"
"$SQLCMD" "${COMMON[@]}" -Q "IF DB_ID('$DB') IS NULL BEGIN CREATE DATABASE [$DB]; PRINT 'created $DB'; END ELSE PRINT '$DB already exists';"

ALREADY=$("$SQLCMD" "${COMMON[@]}" -d "$DB" -h -1 -W \
    -Q "SET NOCOUNT ON; SELECT CASE WHEN OBJECT_ID('sad.Employee','U') IS NULL THEN 'no' ELSE 'yes' END;" | tr -d '[:space:]')

if [ "$ALREADY" = "yes" ]; then
    echo "==> schema already present - skipping schema and seed"
    echo "    (drop the mssql_data volume to rebuild from scratch)"
else
    echo "==> schema"
    "$SQLCMD" "${COMMON[@]}" -d "$DB" -i /database/schema.sql

    echo "==> seed (96k rows, takes a minute)"
    "$SQLCMD" "${COMMON[@]}" -d "$DB" -i /database/seed.sql
fi

# Migrations run on every start, including over an existing volume - each one
# is written to be idempotent, and this is the only thing that brings a
# database created by an older image up to the current schema. Skipping them
# when the schema exists (as this script used to) meant exactly the databases
# that needed a migration were the ones that never got it.
echo "==> migrations"
for f in $(ls /database/migration_*.sql 2>/dev/null | sort); do
    echo "    $(basename "$f")"
    "$SQLCMD" "${COMMON[@]}" -d "$DB" -i "$f"
done

# The application login is created every run: it is cheap, idempotent, and it
# means rotating SAD_DB_PASSWORD in docker/.env takes effect on restart.
#
# Least privilege on purpose - SELECT across the schema, INSERT/UPDATE on only
# the five governance tables the platform actually writes. The CMDB being
# read-only from every layer above SQL Server is then enforced by the
# database, not by convention. No sysadmin, no db_owner, no DDL.
echo "==> application login '$APP_LOGIN'"
"$SQLCMD" "${COMMON[@]}" -d master -Q "
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '$APP_LOGIN')
    CREATE LOGIN [$APP_LOGIN] WITH PASSWORD = '$APP_PASSWORD', CHECK_POLICY = OFF, DEFAULT_DATABASE = [$DB];
ELSE
    ALTER LOGIN [$APP_LOGIN] WITH PASSWORD = '$APP_PASSWORD';
"
"$SQLCMD" "${COMMON[@]}" -d "$DB" -Q "
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '$APP_LOGIN')
    CREATE USER [$APP_LOGIN] FOR LOGIN [$APP_LOGIN];
GRANT SELECT ON SCHEMA::sad TO [$APP_LOGIN];
GRANT INSERT, UPDATE ON sad.Investigation                TO [$APP_LOGIN];
GRANT INSERT, UPDATE ON sad.Conversation                 TO [$APP_LOGIN];
GRANT INSERT         ON sad.ConversationTurn             TO [$APP_LOGIN];
GRANT INSERT, UPDATE ON sad.InfrastructureRecommendation TO [$APP_LOGIN];
GRANT INSERT         ON sad.RecommendationDecision       TO [$APP_LOGIN];
GRANT INSERT         ON sad.CapacityRequest              TO [$APP_LOGIN];
GRANT INSERT, UPDATE ON sad.AgentAuditLog                TO [$APP_LOGIN];
-- Differential indexing watermarks (migration 004). The refresh job reads
-- how far it got last time and writes back how far it got this time, so it
-- needs INSERT and UPDATE. SELECT already comes from the schema grant.
GRANT INSERT, UPDATE ON sad.IndexWatermark              TO [$APP_LOGIN];
-- DELETE too, for this table only. A rebuild clears every watermark so the
-- run that follows behaves as a first index; without it the worker fails
-- with "The DELETE permission was denied" the moment anyone triggers one.
-- Invisible locally, where dev connects as a privileged login - the same
-- way the missing UPDATE on AgentAuditLog hid. Watermarks are bookkeeping
-- the indexer owns outright, not data, so DELETE here grants nothing the
-- application could not already achieve by overwriting them.
GRANT DELETE ON sad.IndexWatermark                      TO [$APP_LOGIN];
-- Index run history (migration 005). The API inserts a Queued run; the
-- worker updates its status, heartbeat and progress. Both processes use
-- this same login, so INSERT and UPDATE cover both. No DELETE: run
-- history is an audit trail of what was indexed and what it cost.
GRANT INSERT, UPDATE ON sad.IndexRun                    TO [$APP_LOGIN];
-- Per-role model selection (migration 006). The admin screen inserts a
-- row the first time a role is overridden and updates it after; Reset
-- deletes it, so DELETE is required here and nowhere else in this schema.
GRANT INSERT, UPDATE, DELETE ON sad.LlmRoleOverride     TO [$APP_LOGIN];
-- Answer and per-call evaluation (migrations 018, 019). Added after both were
-- deployed WITHOUT them and every write failed in silence.
--
-- The schema-wide GRANT SELECT above covers reads, so a new table is readable
-- the moment it exists and writable never - and the failure surfaces nowhere,
-- because answer_evaluation_repository.record deliberately swallows its
-- exception so that grading can never break a delivered answer. Correct design,
-- and it meant the code ran, nothing was stored, and nothing complained. Found
-- on the VM by hand, not by any check.
--
-- No DELETE on either. A verdict is a record of what the platform said at a
-- point in time; deleting one is falsifying an audit trail, not tidying up.
GRANT INSERT ON sad.AnswerEvaluation                    TO [$APP_LOGIN];
GRANT INSERT ON sad.CallEvaluation                      TO [$APP_LOGIN];
-- Remediation queue. In the SAME COMMIT as migration 021, deliberately.
-- 018 created sad.AnswerEvaluation without its grant: SELECT comes from the
-- schema-wide line above and INSERT from nothing, and the repository
-- swallows write failures by design so a verdict cannot break a delivered
-- answer. The platform therefore computed every evaluation, stored none, and
-- reported itself healthy for a whole deploy. Splitting a migration from its
-- grant is what makes that possible.
GRANT INSERT, UPDATE ON sad.RemediationTask             TO [$APP_LOGIN];
-- Evaluation runs (migration 020). UPDATE on EvalRun only, and only because a
-- run is written when it STARTS - so that a crashed run leaves a Running row
-- rather than no evidence it was ever attempted - and finalised when it ends.
-- EvalCaseResult is insert-only: a case result is a fact about one execution.
--
-- Granted in the SAME commit as the migration that creates the tables. 018
-- shipped without its grant, every write failed, and the repository swallowed
-- each failure by design - so the code ran, the table existed, and nothing was
-- stored or reported. Splitting the two is what made that possible.
GRANT INSERT, UPDATE ON sad.EvalRun                     TO [$APP_LOGIN];
GRANT INSERT         ON sad.EvalCaseResult              TO [$APP_LOGIN];
  -- auth.login performs an opportunistic rehash when scrypt parameters change,
  -- which UPDATEs these two columns inside the login request. Without this the
  -- first login after a policy change fails on a *correct* password, and the
  -- error surfaces from the request path rather than at startup. Column-scoped
  -- so the app still cannot touch IsActive, Email or anything else.
  GRANT UPDATE ON sad.Employee(PasswordHash, PasswordUpdatedAt) TO [$APP_LOGIN];

"

echo "==> verification"
"$SQLCMD" "${COMMON[@]}" -d "$DB" -h -1 -W -Q "
SET NOCOUNT ON;
SELECT 'tables   = ' + CAST(COUNT(*) AS VARCHAR) FROM sys.tables WHERE SCHEMA_NAME(schema_id) = 'sad';
SELECT 'clusters = ' + CAST(COUNT(*) AS VARCHAR) FROM sad.InfrastructureCluster;
SELECT 'nodes    = ' + CAST(COUNT(*) AS VARCHAR) FROM sad.ClusterNode;"

echo "==> done"
