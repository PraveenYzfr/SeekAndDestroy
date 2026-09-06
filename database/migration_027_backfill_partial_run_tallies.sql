/*  Runs that stopped instead of finishing reported ZERO cases.

    CasesTotal was written only by complete(), so a run that was aborted, killed
    by a deploy, or crashed reported nothing however much work it had done. Run
    39 was stopped deliberately after 51 cases and read:

        run 39  Error  CasesTotal=0  CasesPassed=0    with 51 EvalCaseResult rows

    Praveen read that as a run that failed instantly. It had run for ten minutes
    and completed just over half the suite. A row saying zero when the answer is
    fifty-one is not missing information, it is wrong information.

    _beat now keeps the tally current on every case, so this cannot recur. This
    migration repairs the rows written before that.

    ONLY ROWS THAT UNDERSTATE THEMSELVES. The WHERE clause requires CasesTotal
    to be null or below the number of results actually recorded, so a completed
    run's own figures are never touched - complete() is the only thing that knows
    a run FINISHED rather than stopped, and its numbers win.

    Idempotent: running it twice changes nothing the second time, because after
    the first pass no row understates itself any more.
*/

SET NOCOUNT ON;
GO

WITH counted AS (
    SELECT  r.EvalRunId,
            COUNT(c.EvalCaseResultId)                                              AS total,
            SUM(CASE WHEN UPPER(c.Outcome) = 'PASSED'  THEN 1 ELSE 0 END)          AS passed,
            SUM(CASE WHEN UPPER(c.Outcome) = 'FAILED'  THEN 1 ELSE 0 END)          AS failed,
            SUM(CASE WHEN UPPER(c.Outcome) = 'SKIPPED' THEN 1 ELSE 0 END)          AS skipped
    FROM    sad.EvalRun r
    JOIN    sad.EvalCaseResult c ON c.EvalRunId = r.EvalRunId
    GROUP BY r.EvalRunId
)
UPDATE  r
SET     CasesTotal   = counted.total,
        CasesPassed  = counted.passed,
        CasesFailed  = counted.failed,
        CasesSkipped = counted.skipped,
        --  Said on the row itself, because a reader seeing 51 of 100 on a run
        --  marked Error must not mistake it for a result. The count is now
        --  true; the run is still not a baseline.
        Notes = CONCAT(ISNULL(r.Notes + ' | ', ''),
                       'Tally backfilled by migration_027 from ',
                       CAST(counted.total AS VARCHAR(10)),
                       ' recorded cases. PARTIAL - this run did not complete.')
FROM    sad.EvalRun r
JOIN    counted ON counted.EvalRunId = r.EvalRunId
WHERE   ISNULL(r.CasesTotal, 0) < counted.total;
GO

PRINT '==> migration_027: partial run tallies backfilled';
GO
