/*  Migration 019 - a score for every model output, not one per answer.

    WHAT ALREADY EXISTED
    --------------------
    sad.AgentAuditLog holds the full exchange for every model call: InputJson
    carries the system and human prompts, OutputJson the parsed result, plus the
    model, tokens, cost and latency. The LLM conversation has been durable all
    along.

    sad.AnswerEvaluation (018) holds ONE row per delivered answer - the aggregate
    NumberFidelity, EntityFidelity and Completeness across every call that
    produced it, plus the judge's verdict.

    WHAT WAS MISSING
    ----------------
    The join between them. An answer scoring 0.91 could be one call at 0.55 and
    four at 1.0, or five at 0.91, and the aggregate cannot tell those apart. When
    a figure is ungrounded there was no way to ask WHICH call invented it, what
    prompt produced it, or which model was serving that role at the time.

    That is the question anyone actually asks of a bad answer, and answering it
    meant re-running the grader and hoping it still behaved the same way.

    ONE ROW PER (CALL, GRADER)
    --------------------------
    number_fidelity, entity_fidelity and completeness each get their own row for
    a given AuditId. Not three columns, because the graders do not all apply to
    every schema: completeness is meaningless for a chain with no required
    narrative fields, and a NULL column cannot distinguish "did not apply" from
    "scored zero" - which is exactly the confusion this platform keeps finding.

    GRADED VALUES ARE STORED, NOT JUST THE RATE
    -------------------------------------------
    Grounded and Total are kept alongside Rate. A rate without its denominator is
    not a measurement: 100% over three mentions and 100% over four hundred are
    different claims, and rounding 2/3 to 0.6667 loses the ability to re-derive
    either. Rate is stored too, because computing it in every query invites one
    caller to divide by zero.

    GRADER VERSION IS PART OF THE KEY
    ---------------------------------
    Five grader changes in one night produced 0.9764, 0.8891 and 0.9740 from the
    SAME calls - identifier tokenisation, an injection hole, a list-count rule.
    Every one of those numbers was correct under the rules in force when it ran,
    and comparing them without knowing which rules applied is meaningless.

    So the uniqueness constraint is (AuditId, Grader, GraderVersion): re-grading
    under new rules ADDS a row rather than overwriting the old verdict, and a
    change in the graders becomes visible as two scores for one call instead of
    one score that silently moved.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.CallEvaluation'))
BEGIN
    CREATE TABLE sad.CallEvaluation
    (
        CallEvaluationId  BIGINT IDENTITY(1,1) NOT NULL,

        --  Which model output this grades. The audit row carries the prompt, the
        --  response, the model identity and the investigation, so everything
        --  needed to explain a score is one join away.
        AuditId           BIGINT           NOT NULL,

        --  Denormalised from the audit row so a conversation-level rollup does
        --  not need a three-table join on every read. Nullable because a call
        --  can belong to no investigation - the chat replies and recalls that
        --  never run the pipeline still produce graded text.
        InvestigationId   INT                  NULL,

        Grader            VARCHAR(40)      NOT NULL,   -- number_fidelity | entity_fidelity | completeness

        --  The measurement, kept whole. See the header: a rate without its
        --  denominator cannot be re-derived or defended.
        Grounded          INT              NOT NULL,
        Total             INT              NOT NULL,
        Rate              DECIMAL(6,4)         NULL,   -- NULL when Total = 0: nothing to score is not zero

        --  The actual offending tokens, so "which number was invented" is
        --  answerable without re-running anything. Capped by the writer.
        UngroundedJson    NVARCHAR(2000)       NULL,

        --  Which rules produced this verdict. Part of the key on purpose.
        GraderVersion     VARCHAR(40)      NOT NULL,

        CreatedAt         DATETIME2(3)     NOT NULL
            CONSTRAINT DF_CallEvaluation_CreatedAt DEFAULT SYSUTCDATETIME(),

        CONSTRAINT PK_CallEvaluation PRIMARY KEY CLUSTERED (CallEvaluationId),
        CONSTRAINT FK_CallEvaluation_Audit FOREIGN KEY (AuditId)
            REFERENCES sad.AgentAuditLog (AuditId),
        --  Re-grading under the SAME rules is idempotent; under new rules it adds
        --  a row. That is the whole point of versioning the verdict.
        CONSTRAINT UQ_CallEvaluation UNIQUE (AuditId, Grader, GraderVersion),
        CONSTRAINT CK_CallEvaluation_Total CHECK (Total >= 0 AND Grounded >= 0 AND Grounded <= Total)
    );
    PRINT 'created sad.CallEvaluation';
END
ELSE PRINT 'sad.CallEvaluation already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CallEvaluation_Investigation')
   AND OBJECT_ID('sad.CallEvaluation') IS NOT NULL
BEGIN
    --  The read path this exists for: "show me this investigation's calls and
    --  what each scored", and the conversation rollup above it.
    CREATE INDEX IX_CallEvaluation_Investigation
        ON sad.CallEvaluation (InvestigationId, Grader)
        WHERE InvestigationId IS NOT NULL;
    PRINT 'created IX_CallEvaluation_Investigation';
END
GO

PRINT '--- migration 019 complete ---';
GO
