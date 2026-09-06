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

# A DIRTY DEPLOY CHECKOUT IS SOMEBODY'S WORK, NOT DEBRIS.
#
# This tree is shared. Sessions run pytest against a change by extracting files
# into it (the runtime image ships no tests directory), and what they leave
# behind aborts the next `git pull --ff-only` hours later, mid-deploy, with the
# person who left it long gone.
#
# The tempting fixes are both wrong. `git checkout -- .` discards it, and on
# 2026-09-04 that would have silently reverted a COMMITTED information-disclosure
# fix in app/agents/query_capability.py - the file looked like litter and was
# not. `git stash` unattended is no better: the build would then run against
# different content than the tree anyone left, and a pop that conflicts ten
# minutes later leaves a half-applied deploy nobody is watching.
#
# So the default is to REFUSE and say exactly what is in the way. --stash-local
# performs the sequence that worked when done by hand, including the check that
# actually matters: the tree must come back byte-identical, or this stops
# rather than reporting a clean deploy over someone's lost work.
STASH_LOCAL=0
for arg in "$@"; do
    case "$arg" in
        --stash-local) STASH_LOCAL=1 ;;
    esac
done

echo "==> 1. pull"
DIRTY=$(git status --porcelain --untracked-files=no)
if [ -n "$DIRTY" ]; then
    echo "    the deploy checkout has uncommitted changes:"
    echo "$DIRTY" | sed "s/^/      /"
    if [ "$STASH_LOCAL" -ne 1 ]; then
        cat <<'HELP'

    Refusing to pull over them. They belong to somebody - verify before you act.

      git -C ~/SeekAndDestroy diff -- <path>     read it first
      bash scripts/deploy-app.sh --stash-local   set aside, deploy, restore, verify

    Never `git checkout -- .` here. On 2026-09-04 that would have reverted a
    committed fix that merely LOOKED like leftover test scaffolding.

    To run tests against a change without dirtying this tree, stage it in /tmp
    and `docker cp` it into the container.
HELP
        exit 1
    fi
    echo "    --stash-local: setting them aside"
    #  TWO RECORDS, FOR TWO DIFFERENT JOBS.
    #
    #  The patch is the verification: what `git diff` said before, compared
    #  against what it says after the pop. Both sides go through git, so a
    #  checkout that normalises line endings cannot make an unchanged file
    #  look changed. A raw byte-for-byte `cp` comparison DID exactly that in
    #  testing - it reported the restore had altered a file it had not
    #  touched, and a check that cries wolf is one somebody switches off.
    #
    #  The file copies are the recovery: if the pop fails or conflicts, the
    #  content is still sitting somewhere a human can read it, which the
    #  stash alone does not guarantee once a conflict has half-applied it.
    BACKUP=$(mktemp -d)
    git diff > "$BACKUP/.before.patch"
    git status --porcelain --untracked-files=no | awk '{print $2}' > "$BACKUP/.files"
    while read -r f; do
        mkdir -p "$BACKUP/$(dirname "$f")" && cp "$f" "$BACKUP/$f"
    done < "$BACKUP/.files"
    git stash push -m "deploy-app.sh set aside $(date -u +%FT%TZ)" >/dev/null
    STASHED=1
    echo "    copies kept in $BACKUP"
    trap 'if [ "${STASHED:-0}" = 1 ]; then
            echo "==> restoring the changes that were set aside"
            if git stash pop >/dev/null 2>&1; then
                git diff > "$BACKUP/.after.patch"
                if cmp -s "$BACKUP/.before.patch" "$BACKUP/.after.patch"; then
                    echo "    restored, and identical to what was set aside:"
                    sed "s/^/      /" "$BACKUP/.files"
                else
                    echo "    RESTORED BUT NOT IDENTICAL - do not assume this deploy was clean."
                    echo "    compare: diff $BACKUP/.before.patch $BACKUP/.after.patch"
                    echo "    originals: $BACKUP"
                fi
            else
                echo "    STASH POP FAILED - the changes are NOT back in the tree."
                echo "    originals: $BACKUP"
                echo "    recover:   git stash list / git stash pop"
            fi
          fi' EXIT
fi
git pull --ff-only
echo "    now at $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

echo "==> 1b. VERSION DIRECTION - a pin must never move a stateful service BACKWARDS"
# WHY THIS EXISTS
#
# 8d2a748 pinned three floating images, which was right - :latest means a
# redeploy can silently change versions and nothing in the repo records what it
# resolved to. Two of the three pins matched what was running. The third set
# grafana to 11.5.1 while production was on 13.2.1: two major versions back, on
# the one service in that commit that keeps state.
#
# The pin recorded an INTENTION where it needed an OBSERVATION. Nothing read the
# running version before writing the number down, and 11.5.1 is exactly the
# shape of a version somebody recalls rather than checks.
#
# It matters here and not for prometheus or alertmanager because Grafana
# migrates its SQLite schema FORWARD and has no downgrade path. An older binary
# opening a newer grafana.db can refuse to start or damage it - and that volume
# holds the dashboards, the datasources, and an admin password this file
# elsewhere notes is only ever applied at FIRST INIT. Losing it is a rebuild,
# not a restart.
#
# So: compare the tag about to be deployed against the tag actually running, and
# REFUSE on a downgrade. Forward moves are allowed silently; that is an upgrade
# and it is the normal case.
#
# Deliberately AFTER the pull, so it reads the compose file that is about to be
# used, and BEFORE any build or recreate, so a refusal costs nothing.
#
# INTERACTION WITH --stash-local, found by the Owner session running exactly
# this by hand: that flag sets a working-tree edit aside BEFORE the pull, so an
# UNCOMMITTED downgrade is invisible here and this prints "unchanged". That is
# not a hole - the deploy then uses the stashed-clean tree, so the downgrade
# never reaches a container either way - but the guard checks what is COMMITTED,
# not what is in the tree, and a test of it must commit the change first.
STATEFUL_SERVICES="grafana prometheus"
for svc in $STATEFUL_SERVICES; do
    container=$(sudo docker ps -a --filter "name=docker-${svc}-1" --format "{{.Names}}" | head -1)
    [ -z "$container" ] && continue

    running=$(sudo docker inspect -f "{{.Config.Image}}" "$container" 2>/dev/null | sed "s/.*://")
    # BOUNDED TO THE SERVICE BLOCK. The first version scanned forward from
    # "  <svc>:" to the first image: line and stopped there - so a service
    # without an image line would silently pick up the NEXT service's tag and
    # compare grafana against alertmanager. All three have one today, which is
    # exactly the kind of "true for now" that this file keeps being bitten by.
    # `exit` on the next top-level key means an absent image line yields empty
    # rather than someone else's version.
    wanted=$(awk -v s="  ${svc}:" '
        $0==s {f=1; next}
        f && /^  [a-zA-Z_]/ {exit}
        f && /^ *image:/ {sub(/.*:/,""); print; exit}
    ' "$REPO/docker/docker-compose.yml")

    # Empty is its own case, not a version. Without this the concatenation below
    # passes the character-class test (nothing invalid in "13.2.1"), the empty
    # string sorts first, and the guard REFUSES with "compose says ." - the
    # right direction for the wrong reason, and a message nobody could act on.
    if [ -z "$wanted" ]; then
        echo "    $svc: no image line found in docker-compose.yml - SKIPPED"
        continue
    fi

    # A tag that is not a version - "latest", a digest, an empty read - cannot be
    # ordered, so it cannot be checked. Said out loud rather than passed over:
    # an unpinned stateful service is the condition this guard was written for,
    # and silence would read as approval.
    case "$running$wanted" in
        *[!0-9.v]*|"") echo "    $svc: cannot compare '$running' -> '$wanted', not both versions - SKIPPED"; continue ;;
    esac
    [ "$running" = "$wanted" ] && { echo "    $svc: $running (unchanged)"; continue; }

    # sort -V orders versions. If the wanted tag sorts FIRST, it is older.
    older=$(printf "%s
%s
" "${running#v}" "${wanted#v}" | sort -V | head -1)
    if [ "$older" = "${wanted#v}" ]; then
        echo ""
        echo "    REFUSING TO DEPLOY."
        echo "    $svc would go BACKWARDS: running $running, compose says $wanted."
        echo ""
        echo "    $svc keeps state. An older binary opening a newer database can"
        echo "    refuse to start or damage it, and there is no downgrade migration."
        echo ""
        echo "    If the downgrade is deliberate, back up the volume first:"
        echo "      sudo docker run --rm -v ${svc}_data:/d -v \$PWD:/b alpine tar czf /b/${svc}-backup.tgz /d"
        echo "    then re-run with ALLOW_DOWNGRADE=1."
        echo ""
        echo "    If it is not deliberate - and it was not last time - read the"
        echo "    running version and pin THAT:"
        echo "      sudo docker inspect -f '{{.Config.Image}}' $container"
        echo ""
        [ "${ALLOW_DOWNGRADE:-0}" = "1" ] || exit 1
        echo "    ALLOW_DOWNGRADE=1 - proceeding anyway."
    else
        echo "    $svc: $running -> $wanted (forward)"
    fi
done

echo "==> 1c. EVALUATION IN FLIGHT - a deploy must not move the ruler mid-measurement"
# WHY THIS EXISTS
#
# On 2026-09-06 at 01:15:58Z this script recreated ai-service while the FIRST
# golden baseline ever run against production was at case 67 of 100. sad.EvalRun
# had been empty; that run was the thing being established.
#
# A restart alone would have been survivable. The deploy also shipped 3a55d90,
# which adds a third deterministic grader - attribution_fidelity - to
# grade_call. So cases 1..67 were scored against two graders and 68..100 against
# three, and any case whose answer misattributes a record fails a check that did
# not exist for the earlier two thirds.
#
# It did not even survive to be split. The recreate KILLED the suite at case 67:
# sad.EvalRun 27 still reads status=Running with FinishedAt NULL and 67 of 100
# case results, and no process remains on the host. A dead run that reports
# itself as running is the worst of the three outcomes - it is neither a result
# nor an obvious failure, and it will sit in that table until someone reads the
# process list to find out it is gone.
#
# Nobody was careless. The deployer was told to deploy, the suite was running in
# another session, and there was no signal between the two. This is that signal.
#
# Checked on the HOST rather than in the database, deliberately: a suite that has
# crashed without writing a completion row would block deploys forever, whereas a
# process either exists or it does not. The cost is that a suite running
# somewhere other than this box is invisible here - a real gap, and a smaller one
# than the alternative.
# THE PATTERN LISTS BOTH NAMES ON PURPOSE. The runner moved into the package as
# app/evaluation/golden_runner.py so it ships inside the image, which means the
# process is now `python -m app.evaluation.golden_runner` and contains no
# "eval_golden" at all. A guard written against the old name would have kept
# passing while a suite ran - silently, and for the same reason the first
# version of it always fired: nobody re-reads a check that is not complaining.
# scripts/eval_golden.py survives as a wrapper and still matches.
#
# SELF AND PARENT EXCLUDED. The first version was `pgrep -f "eval_golden" | wc -l`
# and it matched THE SHELL RUNNING THE CHECK, because on a login shell the
# pattern appears in that shell's own command line. Tested with a pattern
# matching nothing at all and it still refused - a guard that always fires is a
# guard nobody keeps.
#  pgrep EXITS 1 WHEN IT MATCHES NOTHING, and this script runs under
#  `set -euo pipefail`. So on the happy path - no suite running - pgrep's 1
#  propagated through pipefail, the assignment failed, and set -e killed the
#  deploy SILENTLY: step 1c printed its header and nothing after it ran. No
#  error, no refusal, and a caller reading the tail saw a clean-looking stop.
#
#  It shipped that way and blocked the very next deploy. `git pull` had already
#  moved prod's HEAD to 1bdc8b7 while migration_026 never reached the database -
#  the checkout said one thing and the running containers another, which is the
#  half-deployed state this script exists to prevent.
#
#  Same failure as the pgrep-matching-its-own-shell bug caught before it,
#  arriving from the opposite side: that one always REFUSED, this one always
#  ABORTED. A guard has to be correct in the case where it should do NOTHING,
#  and that is the case nobody thinks to test.
# ASKED OF THE RUN ITSELF, NOT OF THE PROCESS TABLE.
#
# The first version grepped for "eval_golden". Then the runner moved into the
# package and the process became `python -m app.evaluation.golden_runner`, which
# contains no such string - so the guard would have kept passing while a suite
# ran. It was caught by grepping for references to the moved path before
# committing, not by any test, because a guard that stops firing looks exactly
# like a guard with nothing to report.
#
# That is the real defect and it is not the pattern: the guard identified its
# subject by a STRING LIVING IN ANOTHER FILE, with nothing enforcing the two
# stayed in step. A rename anywhere silently disarmed it.
#
# The run now declares itself. eval_run_repository beats HeartbeatAt from inside
# record_case - which the runner already calls once per case, so it is not a
# thing a future runner has to remember - and this asks the database. The signal
# is owned by the thing being detected, so it cannot drift out of sync with a
# file that merely mentions it.
#
# TWO MINUTES, and staleness is the point. EvalRun 27 died mid-run and still
# reads Status='Running' with no FinishedAt; a check on Status alone would be
# blocking every deploy on a run that ended hours ago. A heartbeat expires by
# itself, so a killed suite stops blocking without anyone tidying up.
#
# HeartbeatAt IS NULL is the startup window: the row is written before the first
# case, so a suite that has not finished case one is running and has never
# beaten. StartedAt covers it.
#
# THAT SENTENCE IS CONDITIONAL, AND IT USED TO READ AS AN INVARIANT.
#
# Two things were wrong with the version above. The function is repo.start(),
# not open_run - a name that was never in the codebase, so nobody grepping for
# it would find the thing this guard depends on. And the claim is true of the
# CODE PATH while saying nothing about whether that path is reached:
#
#     record = args.record or bool(args.baseline)     # golden_runner.py
#     if record:
#         run_id = repo.start(...)                    # everything above is here
#
# Recording was opt-in. So the invocation everyone actually types -
# `python -m app.evaluation.golden_runner` - wrote NO EvalRun ROW AT ALL. Not a
# Running row, not a partial one: nothing. This guard then found nothing to
# block on and passed, correctly and uselessly, while a hundred-case suite ran.
#
# It cost runs 27 and 40. I diagnosed it as a late write and told two sessions
# the guard was blind; it was neither. The row is not late, it is absent, and
# the guard's premise was sound for every run it could actually see.
#
# a2 has made recording the DEFAULT for a full-suite run (--no-record opts out,
# --case stays unrecorded), which is what restores the premise. NOTE WHAT THAT
# MEANS FOR THIS GUARD: it still cannot see an unrecorded run, and that is
# accepted rather than overlooked. Matching the process name instead was
# considered and rejected - see the paragraph above about a string living in
# another file. That defect is not hypothetical here twice over: the "eval_golden"
# pattern silently stopped matching when the runner moved into the package, and
# an inspection script written while diagnosing THIS comment matched its own
# cmdline, because its source contained the word it was searching for.
EVAL_RUNNING=$(runq <<'SQL' 2>/dev/null | tr -d '[:space:]'
SET NOCOUNT ON;
SELECT COUNT(*) FROM sad.EvalRun
WHERE Status = 'Running'
  AND COALESCE(HeartbeatAt, StartedAt) > DATEADD(MINUTE, -2, SYSUTCDATETIME());
SQL
)
case "$EVAL_RUNNING" in ''|*[!0-9]*) EVAL_RUNNING=0 ;; esac
EVAL_PROCS="$EVAL_RUNNING"

if [ "${EVAL_PROCS:-0}" -gt 0 ]; then
    echo ""
    echo "    REFUSING TO DEPLOY."
    echo "    An evaluation suite is running ($EVAL_PROCS run(s) beating within 2 min)."
    echo ""
    echo "    Recreating ai-service now would split that run across two images."
    echo "    If the deploy also changes a grader, the halves are scored by"
    echo "    different rules and the result cannot be compared with itself."
    echo ""
    echo "    Wait for it to finish. To see it:"
    echo "      SELECT EvalRunId, Status, StartedAt, HeartbeatAt FROM sad.EvalRun"
    echo "       WHERE Status = 'Running' ORDER BY EvalRunId DESC;"
    echo ""
    echo "    If the suite is genuinely stale or you accept a split run, re-run"
    echo "    with ALLOW_DURING_EVAL=1 - and record the split in EvalRun.Notes,"
    echo "    because the next reader cannot see it from the number."
    echo ""
    [ "${ALLOW_DURING_EVAL:-0}" = "1" ] || exit 1
    echo "    ALLOW_DURING_EVAL=1 - proceeding anyway."
else
    echo "    no evaluation suite beating in the last 2 minutes"
fi

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
# THE COMMIT, STAMPED INTO THE IMAGE.
#
# The image ships no .git, so `git rev-parse` inside the container returns
# nothing and sad.EvalRun.GitSha was empty on every containerised run - on rows
# that recorded model config and cost perfectly. A run history that can say the
# score moved but not what moved it fails at the one question it exists for.
#
# PASSED THROUGH sudo EXPLICITLY, NOT EXPORTED. `export SAD_BUILD_SHA=...`
# followed by `sudo docker compose build` does NOT work: sudo runs with
# env_reset, so the variable is stripped before compose interpolates
# ${SAD_BUILD_SHA:-} and the build arg resolves to empty. That failure is
# invisible - the build succeeds, the image is fine, and the column stays empty
# exactly as it was, which reads as "this change did not land" rather than
# "this change landed and sudo threw it away".
#
# Read from the VM's own checkout, which step 1 has already reset to
# origin/main, so it is the sha of what is being BUILT rather than of whatever
# the laptop happened to have.
SAD_BUILD_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "")"
echo "    stamping image with sha ${SAD_BUILD_SHA:-(none - GitSha falls back to git)}"

# --no-deps so restarting ai-service does not take sqlserver, qdrant or redis
# with it. Those hold state this script is explicitly not touching.
( cd "$REPO/docker" \
  && sudo SAD_BUILD_SHA="$SAD_BUILD_SHA" docker compose build ai-service ai-indexer api-gateway ui \
  && sudo docker run --rm --network hub docker-ui:latest nginx -t \
  && sudo docker compose up -d --no-deps ai-service ai-indexer api-gateway ui )

echo "==> 5. observability, if it is running"
# `up -d` rather than `restart`: a changed compose file - a new mount, say -
# needs the container RECREATED. `restart` reuses the old definition and the
# change silently does not land, which looks exactly like a config that does not
# work.
if sudo docker ps --format '{{.Names}}' | grep -q prometheus; then
    ( cd "$REPO/docker" && sudo docker compose --profile observability up -d prometheus grafana alertmanager )
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

echo "==> 6b. GRANT DRIFT - can the app write everywhere it writes?"
# Step 7 below tests two tables by name. This asks the same question of EVERY
# table the repository layer writes to, from one list in the repo.
#
# It exists because a missing grant is SILENT here: every write path swallows its
# exception by design, so a table can be created, written to on every request,
# and stay permanently empty while the platform reports itself healthy. That has
# happened three times - AnswerEvaluation (018), EvalRun (020), RemediationTask
# (021) - and each was found by a person noticing missing data, days later.
#
# Structural cause: GRANT SELECT is schema-wide, INSERT is per table, and there
# are TWO files claiming to issue them. This repository has docker/db-init.sh;
# production runs ~/infra/provision-databases.sh, which is not version
# controlled and has been hand-patched. Until those become one file, this check
# is what stands between a new table and another silent hole.
#
# Non-fatal on purpose. A grant gap must not strand a deploy that is otherwise
# good - the code is already built and the alternative is a half-deployed
# system. It shouts instead.
if ! runq < "$REPO/database/verify_grants.sql" | tee /tmp/grantcheck.out; then
    echo "    grant check could not run - investigate before trusting step 7"
elif grep -qiE "grant missing|TABLE MISSING" /tmp/grantcheck.out; then
    echo "    ^^ GRANT DRIFT: the tables above are unwritable by the app login."
    echo "       Add them to ~/infra/provision-databases.sh on the VM and re-run"
    echo "       'docker compose up db-provision' from ~/infra."
else
    echo "    every writable table is writable"
fi

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

