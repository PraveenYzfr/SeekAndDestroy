/*  Migration 018 - the verdict on each final answer, kept.

    WHAT WAS MISSING
    ----------------
    Every quality check this platform performs already existed and none of it was
    retained.

      * assert_no_number_drift rejected a narration that stated a figure it was
        never given - and recorded that as a log line.
      * graders.number_fidelity / entity_fidelity measured, by arithmetic, what
        share of the figures in an answer came from its evidence - offline, over
        the audit table, only when somebody ran the harness.
      * evaluation.judge scored relevance, groundedness and actionability - only
        from the golden-set runner, never on a real user's answer.

    So the honest answer to "was the report we gave this engineer last Tuesday any
    good" was: nobody knows, and there is no way to find out. The checks ran; the
    verdicts were thrown away.

    This table is where a verdict about a delivered answer lives.

    WHY A ROW PER ANSWER AND NOT A PROMETHEUS COUNTER ALONE
    -------------------------------------------------------
    Prometheus is being used for the rates - it answers "is quality dropping" in
    the shape an alert needs. It cannot answer "show me the four answers that
    scored 2 on groundedness last week", because it stores aggregates and drops
    the individuals. That second question is the one that leads to a fix, so the
    individual verdicts are stored here and the aggregate is derived from them.

    Both are kept deliberately. Neither replaces the other.

    WHAT THIS TABLE DOES NOT CLAIM
    ------------------------------
    JudgeRelevance/Groundedness/Actionability are one model's OPINION of another
    model's answer. They are stored beside the deterministic scores, never merged
    into them, because averaging arithmetic with an opinion produces a number that
    cannot be acted on: a drop could be a fabricated figure or a model being less
    chatty, and only one of those is an incident.

    SelfJudged records that the judge and the author were the same model. It is
    disclosed rather than corrected, because there is no correction - a model
    scoring its own work scores it higher and no prompt fixes that.

    A NULL score is not a zero. A judge that was unavailable, sampled out, or
    unable to grade leaves NULLs and a JudgeError, so an average over this table
    cannot quietly count a missing verdict as a bad one.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF OBJECT_ID('sad.AnswerEvaluation', 'U') IS NULL
BEGIN
    CREATE TABLE sad.AnswerEvaluation
    (
        AnswerEvaluationId  INT IDENTITY(1,1) NOT NULL,

        -- What was evaluated. InvestigationId is NULL for the answers that never
        -- create one - a greeting, a request too vague to act on, a reference
        -- with nothing to refer to. Those are still answers this platform gave
        -- and are still worth grading.
        InvestigationId     INT               NULL,
        ConversationId      CHAR(32)          NULL,

        -- The question as the user asked it, truncated. Kept because a low score
        -- is uninterpretable without it: "groundedness 2" says nothing until you
        -- can see that the question asked for something the evidence never had.
        Question            NVARCHAR(2000)    NULL,

        -- =====================================================================
        -- Deterministic. Arithmetic, not opinion.
        -- =====================================================================
        -- Share of figures / entity codes in the prose that appear in the
        -- evidence the prose was written from. NULL means NOT MEASURED - the
        -- prompt was truncated or the evidence was unrecoverable - and is
        -- deliberately distinct from 0.0, which means every figure was invented.
        NumberFidelity      DECIMAL(5,4)      NULL,
        EntityFidelity      DECIMAL(5,4)      NULL,
        -- Share of the schema's REQUIRED fields the model actually filled in.
        -- Kept even though it needs no evidence, and that is the point: it is
        -- the one deterministic score still measurable when a prompt was
        -- truncated, so an answer with unrecoverable evidence is not completely
        -- ungraded.
        Completeness        DECIMAL(5,4)      NULL,
        -- The individual offending tokens, so a failure can be read rather than
        -- only counted. JSON array, capped by the writer.
        UngroundedJson      NVARCHAR(MAX)     NULL,
        -- Calls that could not be graded at all, counted rather than dropped: a
        -- fidelity rate over an unstated subset is worse than no rate.
        GradedCalls         INT               NOT NULL CONSTRAINT DF_AnswerEvaluation_Graded DEFAULT 0,
        UngradeableCalls    INT               NOT NULL CONSTRAINT DF_AnswerEvaluation_Ungradeable DEFAULT 0,

        -- =====================================================================
        -- LLM judge. Opinion, labelled as such.
        -- =====================================================================
        JudgeProvider       VARCHAR(40)       NULL,
        JudgeModel          NVARCHAR(200)     NULL,
        JudgeRelevance      TINYINT           NULL,
        JudgeGroundedness   TINYINT           NULL,
        JudgeActionability  TINYINT           NULL,
        -- The judge's own view of whether it had enough to grade on. A judge that
        -- says it could not tell is more useful than one that guesses.
        JudgeConfident      BIT               NULL,
        JudgeSelfJudged     BIT               NULL,
        -- Free text quoting the phrase each score reacted to. A justification
        -- that names nothing is unfalsifiable, so the prompt demands a quote and
        -- this is where it is kept.
        JudgeJustification  NVARCHAR(MAX)     NULL,
        -- Why there is no verdict. Populated exactly when the scores are NULL.
        JudgeError          NVARCHAR(500)     NULL,

        DurationMs          INT               NULL,
        CreatedAt           DATETIME2(3)      NOT NULL
            CONSTRAINT DF_AnswerEvaluation_CreatedAt DEFAULT SYSUTCDATETIME(),

        CONSTRAINT PK_AnswerEvaluation PRIMARY KEY CLUSTERED (AnswerEvaluationId),
        CONSTRAINT FK_AnswerEvaluation_Investigation FOREIGN KEY (InvestigationId)
            REFERENCES sad.Investigation (InvestigationId),
        -- Scores are 1-5 by definition of the rubric. Enforced here so a parsing
        -- change upstream cannot quietly widen the scale and make last month's
        -- numbers incomparable with this month's.
        CONSTRAINT CK_AnswerEvaluation_Relevance
            CHECK (JudgeRelevance IS NULL OR JudgeRelevance BETWEEN 1 AND 5),
        CONSTRAINT CK_AnswerEvaluation_Groundedness
            CHECK (JudgeGroundedness IS NULL OR JudgeGroundedness BETWEEN 1 AND 5),
        CONSTRAINT CK_AnswerEvaluation_Actionability
            CHECK (JudgeActionability IS NULL OR JudgeActionability BETWEEN 1 AND 5),
        CONSTRAINT CK_AnswerEvaluation_NumberFidelity
            CHECK (NumberFidelity IS NULL OR NumberFidelity BETWEEN 0 AND 1),
        CONSTRAINT CK_AnswerEvaluation_EntityFidelity
            CHECK (EntityFidelity IS NULL OR EntityFidelity BETWEEN 0 AND 1),
        CONSTRAINT CK_AnswerEvaluation_Completeness
            CHECK (Completeness IS NULL OR Completeness BETWEEN 0 AND 1)
    );

    -- The two questions this table is read for: "how has quality moved" (time
    -- order) and "show me the answers for this investigation" (lookup).
    CREATE INDEX IX_AnswerEvaluation_CreatedAt
        ON sad.AnswerEvaluation (CreatedAt DESC);
    CREATE INDEX IX_AnswerEvaluation_Investigation
        ON sad.AnswerEvaluation (InvestigationId)
        WHERE InvestigationId IS NOT NULL;
END
GO
