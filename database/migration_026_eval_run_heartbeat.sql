/*  Migration 026 - tell an abandoned eval run from a live one.

    WHAT HAPPENED
    -------------
    2026-09-06 00:55. The first golden baseline ever recorded against production
    started. At 01:15 a deploy recreated ai-service; the process died at case 67
    of 100 and its log went with the container.

    sad.EvalRun #27 was left in status Running with no FinishedAt. FOREVER. The
    row is written at START - deliberately, so a crashed suite leaves evidence it
    was attempted rather than vanishing - but nothing ever revisits it, so an
    aborted run and a live one are distinguishable only by squinting at
    StartedAt and guessing.

    That is not a cosmetic problem. It cost twice:

      - somebody could read 67 partial cases as a result, or pin them as a
        baseline, and the row gives no hint that they are partial;
      - a deploy guard that checks "is an eval running?" against this table
        would then block EVERY FUTURE DEPLOY on a run that died an hour ago.
        a2 wrote their guard against the host process list instead, precisely
        to avoid that - which is correct, and also means the table stays wrong.

    THE HEARTBEAT
    -------------
    Updated by record_case(), which the runner already calls once per case, so
    it costs no new discipline and cannot be forgotten by a future runner. A live
    run beats roughly every 15 seconds; a run that has not beaten in fifteen
    MINUTES has not completed a single case in that time and is dead.

    Nothing is ever deleted. A stale run is marked Error with a note saying it
    was presumed killed, because a row that disappears is indistinguishable from
    a run nobody ever started - which is the failure this table was created to
    prevent in the first place.
*/
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;
GO

IF COL_LENGTH('sad.EvalRun', 'HeartbeatAt') IS NULL
BEGIN
    ALTER TABLE sad.EvalRun ADD
        --  Last sign of life. NULL on a run that died before its first case
        --  completed, which is why the sweep falls back to StartedAt rather
        --  than treating NULL as "never stale".
        HeartbeatAt DATETIME2(3) NULL;
    PRINT 'added sad.EvalRun.HeartbeatAt';
END
ELSE PRINT 'sad.EvalRun.HeartbeatAt already present - skipped';
GO

--  Existing Running rows have no heartbeat and never will - the code that would
--  write one did not exist when they were created. Left alone deliberately: the
--  sweep falls back to StartedAt, so they age out on their own the next time a
--  run starts, and back-filling a heartbeat here would invent a sign of life
--  that was never observed.
