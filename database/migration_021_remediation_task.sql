/*  Migration 021 - the failures that currently vanish.

    WHAT HAPPENS TODAY
    ------------------
    Seven places in app/graph/nodes.py catch an exception, log a warning, and
    drop it:

        explain_candidate_failed          explain_rightsizing_failed
        grounded_qa_failed                retrieve_related_context_failed
        generate_final_report_failed      select_candidate_nodes_failed
        load_application_requirements.llm_extraction_failed

    Every one of those is correct in what it DOES - a narration failure must not
    fail an investigation whose numbers are already computed. What is wrong is
    that nothing counts them, nothing stores them, and nothing surfaces them. An
    investigation returns 200 with a candidate silently missing its explanation,
    and "how often does narration fail" is answerable only by grepping container
    logs on a box.

    That is the fourth failure of this exact shape found in a day: Grafana
    serving an unauthenticated dashboard behind a 200, containers stuck on old
    images while the site returned 200, an empty review screen on a 200, and
    this. In each case the signal said "fine" because nothing had asked the
    question that mattered.

    "Best effort" describes what the code should DO about a failure. It does not
    decide whether anyone should be TOLD.

    WHY A TABLE AND NOT JUST A COUNTER
    ----------------------------------
    A counter answers "how often". It cannot answer "which answer, what was the
    model given, what did it say, and is it still wrong" - and those are the
    questions that make a failure fixable. Prometheus gets the rate; this gets
    the case.

    THE GRANT IS IN THIS COMMIT, IN docker/db-init.sh
    -------------------------------------------------
    Not stylistic. Migration 018 created sad.AnswerEvaluation without its INSERT
    grant. db-init.sh grants SELECT schema-wide and INSERT per table, so a new
    table is readable immediately and writable never. The repository swallows
    write failures by design - a verdict must not break a delivered answer - so
    the platform computed every evaluation, stored none, and reported itself
    healthy for an entire deploy. Splitting the migration from its grant is what
    made that possible.

    NOTHING ACTS ON THESE ROWS YET
    ------------------------------
    Queue and read only. The triage taxonomy below is a guess until there are
    real failures to check it against, and an agent built on a guessed taxonomy
    would confidently mis-route fifty cases before anyone noticed.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.RemediationTask'))
BEGIN
    CREATE TABLE sad.RemediationTask
    (
        RemediationTaskId  BIGINT IDENTITY(1,1) NOT NULL,

        --  Where it happened. Both nullable: the chat replies, recalls and
        --  refusals that never run the pipeline still produce failures, and a
        --  failure with no investigation is not less real than one with.
        InvestigationId    INT                  NULL,
        ConversationId     CHAR(32)             NULL,

        --  WHICH DROP SITE. The logger event name, unchanged, so a row can be
        --  traced back to the exact except branch that produced it without a
        --  translation table that will drift from the code.
        Site               VARCHAR(80)      NOT NULL,

        --  'python' for an exception caught in the graph, 'judge' for a low
        --  LLM-as-judge verdict. The judge path lands behind this one: every
        --  role currently runs on deepseek-v4-flash, so the judge is always the
        --  author and every verdict is discarded as self-judged.
        Source             VARCHAR(20)      NOT NULL,

        --  A GUESS, and labelled as one. Filled in later by whoever triages;
        --  NULL means nobody has classified it, which is different from
        --  classified-as-unknown and must stay distinguishable.
        TriageClass        VARCHAR(60)          NULL,

        --  The exception, or the judge's complaint.
        Detail             NVARCHAR(2000)       NULL,

        --  What the answer said and what the engine had given it. Both, because
        --  a failure is not diagnosable from either alone - the question is
        --  always whether the model departed from what it was handed.
        AnswerText         NVARCHAR(MAX)        NULL,
        EvidenceJson       NVARCHAR(MAX)        NULL,

        --  Scores AND justifications. A bare 2/5 says something is wrong and
        --  nothing about what, so the judge's own words are stored beside its
        --  numbers or the row cannot be acted on.
        JudgeRelevance     TINYINT              NULL,
        JudgeGroundedness  TINYINT              NULL,
        JudgeActionability TINYINT              NULL,
        JudgeJustifications NVARCHAR(MAX)       NULL,

        --  How many times this has been through the loop. Present now so a
        --  retrying agent later cannot be built without it and quietly retry
        --  forever.
        Attempt            INT              NOT NULL
            CONSTRAINT DF_RemediationTask_Attempt DEFAULT 0,

        Status             VARCHAR(20)      NOT NULL
            CONSTRAINT DF_RemediationTask_Status DEFAULT 'Queued',

        CreatedAt          DATETIME2(3)     NOT NULL
            CONSTRAINT DF_RemediationTask_CreatedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt          DATETIME2(3)         NULL,

        CONSTRAINT PK_RemediationTask PRIMARY KEY CLUSTERED (RemediationTaskId),
        --  NOT a foreign key to sad.Investigation on purpose. A failure that
        --  happened while an investigation row was being written must still be
        --  recordable; a constraint here would drop exactly the failures that
        --  occur at the worst moment.
        CONSTRAINT CK_RemediationTask_Status CHECK (Status IN
            ('Queued', 'Triaged', 'Retrying', 'Resolved', 'Escalated', 'Abandoned')),
        CONSTRAINT CK_RemediationTask_Source CHECK (Source IN ('python', 'judge')),
        CONSTRAINT CK_RemediationTask_Attempt CHECK (Attempt >= 0)
    );
    PRINT 'created sad.RemediationTask';
END
ELSE PRINT 'sad.RemediationTask already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_RemediationTask_Queue')
   AND OBJECT_ID('sad.RemediationTask') IS NOT NULL
BEGIN
    --  The read this exists for: the open queue, newest first.
    CREATE INDEX IX_RemediationTask_Queue
        ON sad.RemediationTask (Status, RemediationTaskId DESC);
    PRINT 'created IX_RemediationTask_Queue';
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_RemediationTask_Site')
   AND OBJECT_ID('sad.RemediationTask') IS NOT NULL
BEGIN
    --  "How often does narration fail, and where" - the question that needed a
    --  log grep on a production box.
    CREATE INDEX IX_RemediationTask_Site
        ON sad.RemediationTask (Site, CreatedAt);
    PRINT 'created IX_RemediationTask_Site';
END
GO

PRINT '--- migration 021 complete ---';
GO
