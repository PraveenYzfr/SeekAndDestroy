/*  Migration 020 - what the evaluation suite scored, and under what.

    WHAT WAS MISSING
    ----------------
    Every evaluation this platform can run prints to a terminal and is lost.

        scripts/eval_golden.py     10 golden cases, hard checks and a judge
        scripts/eval_retrieval.py  dense vs sparse vs hybrid
        scripts/evaluate.py        fidelity over the audit table
        scripts/eval_ablation.py   does the CMDB corpus earn its place

    All four produce real numbers and none of them keeps one. So "did quality
    improve when we moved narration to Groq" is answerable only by whoever still
    has the scrollback, and "has anything regressed since last month" is not
    answerable at all.

    sad.AnswerEvaluation (018) and sad.CallEvaluation (019) record what happened
    to answers users actually asked for. This is the other axis: what a FIXED set
    of questions scored, on purpose, so two runs can be compared.

    A SCORE WITHOUT ITS CONFIGURATION IS NOT A RESULT
    -------------------------------------------------
    ModelsJson stores the provider and model serving every role at the moment the
    run started, and GitSha the commit it ran from.

    Without those a stored score is a number with no experiment attached. "0.94"
    means nothing; "0.94, narration on groq/gpt-oss-20b, everything else on
    deepseek-v4-flash, at 678daf8" is a result somebody can reproduce or dispute.
    That is the whole difference between a measurement and a memory.

    Roles are read at run START, not per case, because a role repointed halfway
    through produces a run whose configuration cannot be stated - and a result
    that cannot be stated should not be stored as though it can.

    THE BASELINE IS PINNED, NOT "THE PREVIOUS RUN"
    ----------------------------------------------
    BaselineRunId names the run this one was judged against, and it is chosen
    deliberately rather than defaulting to whatever ran last.

    A gate that compares each run to its predecessor permits unlimited drift in
    small steps: every run passes against a slightly worse one, the bar descends
    continuously, and no single comparison ever fails. Pinning means the bar
    moves only when a person moves it, and moving it is a visible act rather than
    a side effect of time passing.

    VERDICT IS STORED, NOT DERIVED
    ------------------------------
    Whether a run passed is written down at the time, not recomputed later from
    the case rows. The thresholds can change - they have, five times in one night
    when the graders were being fixed - and recomputing an old run under today's
    rules produces a verdict nobody ever acted on. What was decided is a fact
    about that moment.

    NOTHING HERE IS EVER UPDATED except the finalisation of a run in flight
    (Status, FinishedAt, the totals and the verdict). There is no path that
    edits a completed run and none should be added: an evaluation record that
    can be revised is not evidence.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.EvalRun'))
BEGIN
    CREATE TABLE sad.EvalRun
    (
        EvalRunId       INT IDENTITY(1,1) NOT NULL,

        --  golden | retrieval | insights | audit - which suite ran. Runs are only
        --  ever compared within a suite; a golden score and a retrieval score are
        --  not the same kind of number.
        Suite           VARCHAR(40)       NOT NULL,

        --  Running | Passed | Failed | Regressed | Error.
        --
        --  Failed and Regressed are deliberately distinct. Failed means this run
        --  broke a rule that stands on its own - a hard check, a floor. Regressed
        --  means it is worse than the baseline while still inside every absolute
        --  limit. They need different responses: one is a bug, the other is a
        --  trade somebody may have chosen to make.
        Status          VARCHAR(20)       NOT NULL,

        --  The configuration. See the header: a score without these is a number
        --  with no experiment attached.
        GitSha          VARCHAR(40)           NULL,
        ModelsJson      NVARCHAR(MAX)         NULL,

        --  What was compared against, and NULL when nothing was - the first run
        --  of a suite has no baseline and must not be recorded as though it
        --  passed one.
        BaselineRunId   INT                   NULL,

        --  Totals, stored rather than counted from the case rows on read. The
        --  case rows are the detail; these are what the run CONCLUDED, and a
        --  count taken later under different filtering can disagree with it.
        CasesTotal      INT               NOT NULL CONSTRAINT DF_EvalRun_Total   DEFAULT 0,
        CasesPassed     INT               NOT NULL CONSTRAINT DF_EvalRun_Passed  DEFAULT 0,
        CasesFailed     INT               NOT NULL CONSTRAINT DF_EvalRun_Failed  DEFAULT 0,
        --  Neither passed nor failed: no evidence to grade against, a truncated
        --  prompt, a provider that was down. Counted separately because folding
        --  them into either column reports a measurement that was not made.
        CasesSkipped    INT               NOT NULL CONSTRAINT DF_EvalRun_Skipped DEFAULT 0,

        --  Headline numbers, nullable because a suite that could not measure one
        --  must leave it absent rather than zero.
        HardCheckRate   DECIMAL(6,4)          NULL,
        JudgeMeanScore  DECIMAL(4,2)          NULL,
        --  Judge verdicts excluded as self-judged. A mean over 3 independent
        --  cases and a mean over 40 are different claims, and this is what says
        --  which one is on the row above.
        JudgeExcluded   INT               NOT NULL CONSTRAINT DF_EvalRun_JExcl   DEFAULT 0,

        --  Why the verdict was what it was, in words, written at the time.
        Notes           NVARCHAR(2000)        NULL,

        TriggeredBy     NVARCHAR(100)         NULL,   -- employee number, or 'ci'
        StartedAt       DATETIME2(3)      NOT NULL
            CONSTRAINT DF_EvalRun_StartedAt DEFAULT SYSUTCDATETIME(),
        FinishedAt      DATETIME2(3)          NULL,

        CONSTRAINT PK_EvalRun PRIMARY KEY CLUSTERED (EvalRunId),
        --  Self-referencing: a baseline is just an earlier run of the same suite.
        CONSTRAINT FK_EvalRun_Baseline FOREIGN KEY (BaselineRunId)
            REFERENCES sad.EvalRun (EvalRunId),
        CONSTRAINT CK_EvalRun_Status CHECK
            (Status IN ('Running', 'Passed', 'Failed', 'Regressed', 'Error')),
        CONSTRAINT CK_EvalRun_Counts CHECK
            (CasesTotal >= 0 AND CasesPassed >= 0 AND CasesFailed >= 0 AND CasesSkipped >= 0),
        CONSTRAINT CK_EvalRun_HardRate CHECK
            (HardCheckRate IS NULL OR HardCheckRate BETWEEN 0 AND 1),
        CONSTRAINT CK_EvalRun_Judge CHECK
            (JudgeMeanScore IS NULL OR JudgeMeanScore BETWEEN 1 AND 5)
    );
    CREATE INDEX IX_EvalRun_Suite ON sad.EvalRun (Suite, StartedAt DESC);
    PRINT 'created sad.EvalRun';
END
ELSE PRINT 'sad.EvalRun already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.EvalCaseResult'))
BEGIN
    CREATE TABLE sad.EvalCaseResult
    (
        EvalCaseResultId BIGINT IDENTITY(1,1) NOT NULL,
        EvalRunId        INT               NOT NULL,

        --  The case id from golden_set.yaml. Stable across runs on purpose -
        --  it is the join that makes "which cases got worse" answerable, and
        --  renaming one silently breaks every comparison that spans the change.
        CaseId           VARCHAR(80)       NOT NULL,
        CaseKind         VARCHAR(40)           NULL,

        --  Passed | Failed | Skipped. Skipped is not a pass: a case that could
        --  not run tells you nothing about quality, and counting it either way
        --  reports a measurement that was never taken.
        Outcome          VARCHAR(20)       NOT NULL,

        --  Which hard checks ran and which failed, as recorded - so a failure
        --  can be read a month later without re-running anything.
        HardChecksJson   NVARCHAR(MAX)         NULL,

        JudgeRelevance     TINYINT             NULL,
        JudgeGroundedness  TINYINT             NULL,
        JudgeActionability TINYINT             NULL,
        --  Same disclosure as sad.AnswerEvaluation: reported, never corrected.
        JudgeSelfJudged    BIT                 NULL,

        --  Truncated. Enough to see WHAT the platform said, not a transcript -
        --  the audit log holds the full exchange and this is a summary row.
        AnswerExcerpt    NVARCHAR(2000)        NULL,
        ErrorMessage     NVARCHAR(500)         NULL,
        DurationMs       INT                   NULL,

        CONSTRAINT PK_EvalCaseResult PRIMARY KEY CLUSTERED (EvalCaseResultId),
        CONSTRAINT FK_EvalCaseResult_Run FOREIGN KEY (EvalRunId)
            REFERENCES sad.EvalRun (EvalRunId),
        --  One result per case per run. A retry within a run overwrites rather
        --  than appending a second, contradictory verdict for the same case.
        CONSTRAINT UQ_EvalCaseResult UNIQUE (EvalRunId, CaseId),
        CONSTRAINT CK_EvalCaseResult_Outcome CHECK
            (Outcome IN ('Passed', 'Failed', 'Skipped')),
        CONSTRAINT CK_EvalCaseResult_Judge CHECK
            ((JudgeRelevance     IS NULL OR JudgeRelevance     BETWEEN 1 AND 5)
         AND (JudgeGroundedness  IS NULL OR JudgeGroundedness  BETWEEN 1 AND 5)
         AND (JudgeActionability IS NULL OR JudgeActionability BETWEEN 1 AND 5))
    );
    --  The comparison query this table exists for: line up two runs by CaseId
    --  and show what changed.
    CREATE INDEX IX_EvalCaseResult_Case ON sad.EvalCaseResult (CaseId, EvalRunId);
    PRINT 'created sad.EvalCaseResult';
END
ELSE PRINT 'sad.EvalCaseResult already present - skipped';
GO

PRINT '--- migration 020 complete ---';
GO
