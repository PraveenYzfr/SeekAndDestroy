/*  Migration 025 - what a golden run COST, not only what it scored.

    WHAT IS MISSING WITHOUT IT
    --------------------------
    sad.EvalRun is the one table designed to answer "same 100 queries, what
    changed?". It records CasesPassed, HardCheckRate, JudgeMeanScore, the git sha
    and the model configuration.

    It records nothing about spend, tokens or time.

    So the champion/challenger question - the reason the table exists - can only
    be half answered. A run can be shown to be BETTER. It cannot be shown to be
    cheaper, dearer, faster or slower, and for a model swap that is most of the
    decision. "Worse but a third of the price" and "worse for no saving" are
    different answers and the table cannot tell them apart.

    WHY THESE ARE SUMMED, NOT PRICED
    --------------------------------
    Every figure here is summed from sad.AgentAuditLog over the run's own window.
    Cost is priced AT CALL TIME from sad.ModelPrice and copied onto the audit
    row; nothing re-derives it later. A price change must not be able to rewrite
    what last month's run cost, and re-deriving is exactly how it would.

    UnpricedCalls IS NOT OPTIONAL AND IS THE POINT OF THIS MIGRATION AS MUCH AS
    CostUsd. On 2026-09-06, deepseek-v4-flash was 37 of 63 live calls and NONE
    of them priced. A run over that estate would have stored a small, plausible,
    confident cost that was missing the majority of its own traffic. A cost of
    zero looks broken and gets investigated; a cost that is quietly 40% short
    does not.

    So the column exists to make an incomplete number visibly incomplete. A run
    with UnpricedCalls > 0 has a CostUsd that is a FLOOR, not a total, and any
    comparison against another run must say so.

    DurationMs is wall clock for the whole run, not the sum of the calls. A run
    that parallelises and one that does not can burn identical model time and
    take very different amounts of somebody's afternoon, and the eval gate is
    something a person waits for.
*/
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;
GO

IF COL_LENGTH('sad.EvalRun', 'CostUsd') IS NULL
BEGIN
    ALTER TABLE sad.EvalRun ADD
        --  Summed from AgentAuditLog.CostUsd over the run window. NULL when the
        --  sum could not be taken at all, which is not the same as 0.00 - see
        --  UnpricedCalls for the partial case.
        CostUsd       DECIMAL(12,6)     NULL,
        TokensIn      INT               NULL,
        TokensOut     INT               NULL,
        --  Wall clock for the run. EvalCaseResult already holds per-case
        --  DurationMs; this is what a person actually waited.
        DurationMs    INT               NULL,
        --  Calls in the window whose model had no price in force. CostUsd is a
        --  FLOOR whenever this is above zero, and any run-to-run comparison has
        --  to say so rather than presenting a short total as a real one.
        UnpricedCalls INT               NOT NULL
            CONSTRAINT DF_EvalRun_Unpriced DEFAULT 0;
    PRINT 'added EvalRun spend columns';
END
ELSE PRINT 'EvalRun spend columns already present - skipped';
GO
