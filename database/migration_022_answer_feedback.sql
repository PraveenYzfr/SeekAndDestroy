/*  Migration 022 - what the person who read the answer thought of it.

    WHAT IS MISSING WITHOUT IT
    --------------------------
    Every quality signal in this platform is machine-generated.

        number/entity fidelity   arithmetic over evidence
        completeness             fields present or not
        the judge                one model's opinion of another's work

    None of them has ever been checked against a human. The judge in particular
    is evidence about an answer, not a measurement, and nothing anywhere tells
    us whether its opinion tracks the opinion of the engineer who acted on the
    report. A judge that scores confidently and wrongly looks exactly like a
    judge that scores confidently and well.

    This table is the only ground truth in the system. It is also the only way
    to answer "is the judge worth what it costs" with something other than an
    argument.

    RATING IS -1, 0 or +1, NOT 1-5
    -------------------------------
    A five-point scale invites a considered judgement and gets a shrug. Nobody
    reading a capacity report wants to weigh relevance against actionability;
    they want to say it helped or it did not, in one click, and go back to work.
    A scale people will not use produces no data, and no data is worse than
    coarse data.

    0 is not neutral-as-in-average. It is "I looked and I am not sure", which is
    a real answer and a different one from having no row at all.

    WHY THE REASON IS A FIXED SET AND NOT FREE TEXT
    -----------------------------------------------
    Free text cannot be counted, routed or compared across a hundred answers.
    The reasons here map ONTO THE SAME TAXONOMY the remediation triage uses -
    wrong numbers, missing evidence, did not answer the question, not actionable
    - so a human verdict and a machine verdict become directly comparable
    instead of living in separate vocabularies.

    Comment is kept as well, and it is where the thing nobody predicted gets
    said. It is read, never aggregated.

    ONE ROW PER PERSON PER INVESTIGATION
    -------------------------------------
    A FILTERED unique index, not a plain constraint, and the difference is not
    cosmetic. SQL Server treats NULLs as EQUAL for uniqueness, so a plain UNIQUE
    (InvestigationId, EmployeeId) would let a person rate exactly ONE
    conversational reply ever - every one of those has a NULL investigation id
    and the second collapses onto the first. Filtering to
    WHERE InvestigationId IS NOT NULL applies the rule only where there is an
    identity to apply it to.

    A person may change their mind - the row is updated - but they cannot vote
    twice on the same investigation, and two engineers disagreeing about one
    report is a fact worth keeping rather than a conflict to resolve.

    NOT ANONYMOUS, and that is deliberate. A rating whose author is unknown
    cannot be followed up, and "the reviewer who rejected this can be asked why"
    is most of the value. EmployeeId is the same identity the investigation was
    created under.

    THE GRANT IS IN THIS COMMIT. Migration 018 shipped without one, every write
    failed, the repository swallowed it by design, and the platform reported
    itself healthy for a whole deploy. Splitting the two is what made that
    possible, so they are not split.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.AnswerFeedback'))
BEGIN
    CREATE TABLE sad.AnswerFeedback
    (
        AnswerFeedbackId INT IDENTITY(1,1) NOT NULL,

        --  What was rated. NOT a foreign key to Investigation for the same
        --  reason sad.RemediationTask is not: a conversational reply that never
        --  created an Investigation row is still an answer this platform gave,
        --  and still worth rating. Constraining it here would collect feedback
        --  only on the answers that already went well enough to get a row.
        InvestigationId  INT               NULL,
        ConversationId   CHAR(32)          NULL,

        --  Who. Not anonymous on purpose - see the header.
        EmployeeId       INT               NOT NULL,

        --  -1 unhelpful | 0 unsure | +1 helpful
        Rating           SMALLINT          NOT NULL,

        --  One of the fixed reasons, mapped onto the remediation taxonomy so a
        --  human verdict and a machine verdict are directly comparable.
        --  Nullable: a thumbs-up rarely has a reason and demanding one is how a
        --  feedback control stops being used.
        Reason           VARCHAR(40)           NULL,

        --  Read, never aggregated. Where the thing nobody predicted gets said.
        Comment          NVARCHAR(2000)        NULL,

        --  What the machine thought of the SAME answer, copied at rating time.
        --
        --  Denormalised deliberately. The judge's verdict can be recomputed and
        --  the graders can be fixed - both happened repeatedly in one night -
        --  and a comparison of human against machine is meaningless if the
        --  machine half silently changes afterwards. This is what the judge said
        --  when this person disagreed with it.
        JudgeMinScore    TINYINT               NULL,
        NumberFidelity   DECIMAL(5,4)          NULL,

        CreatedAt        DATETIME2(3)      NOT NULL
            CONSTRAINT DF_AnswerFeedback_CreatedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt        DATETIME2(3)          NULL,

        CONSTRAINT PK_AnswerFeedback PRIMARY KEY CLUSTERED (AnswerFeedbackId),
        CONSTRAINT FK_AnswerFeedback_Employee FOREIGN KEY (EmployeeId)
            REFERENCES sad.Employee (EmployeeId),
        --  Uniqueness is a FILTERED INDEX below, not a constraint here.
        --
        --  A plain UNIQUE (InvestigationId, EmployeeId) is wrong in two ways at
        --  once, and they cancel out into silence. SQL Server treats NULLs as
        --  EQUAL for uniqueness, so every conversational reply - which has no
        --  InvestigationId - collapses to one row per person: rate one, and the
        --  second is rejected forever. Meanwhile NULL = NULL is FALSE in a join,
        --  so the upsert's ON clause never matches those rows and always tries
        --  to insert. A person rating a second chat answer got a constraint
        --  violation from an upsert that was supposed to update.
        --
        --  Found by exercising it, not by reading it.
        CONSTRAINT CK_AnswerFeedback_Rating CHECK (Rating IN (-1, 0, 1)),
        --  The same vocabulary the remediation triage uses. A reason outside it
        --  is rejected rather than stored, because a taxonomy that accepts
        --  anything is free text wearing a column name.
        CONSTRAINT CK_AnswerFeedback_Reason CHECK (Reason IS NULL OR Reason IN (
            'wrong_numbers',        -- a figure was wrong
            'wrong_entity',         -- named a cluster or app that does not fit
            'missing_evidence',     -- asked for something we do not record
            'did_not_answer',       -- answered a different question
            'not_actionable',       -- correct and I cannot act on it
            'too_slow',             -- right answer, took too long to be useful
            'other'
        ))
    );

    --  The two reads this exists for: "what did people think this week" and
    --  "show me everything this person rated badly".
    --  One vote per person per INVESTIGATION. Filtered, so the rule applies
    --  only where there is an id to key on - conversational replies are not
    --  constrained, because "no investigation" is not an identity two rows can
    --  share.
    CREATE UNIQUE INDEX UX_AnswerFeedback_Investigation
        ON sad.AnswerFeedback (InvestigationId, EmployeeId)
        WHERE InvestigationId IS NOT NULL;

    CREATE INDEX IX_AnswerFeedback_Created ON sad.AnswerFeedback (CreatedAt DESC);
    CREATE INDEX IX_AnswerFeedback_Rating  ON sad.AnswerFeedback (Rating, CreatedAt DESC);
    PRINT 'created sad.AnswerFeedback';
END
ELSE PRINT 'sad.AnswerFeedback already present - skipped';
GO

/*  Golden-set promotion: which real failure a case came from.

    A remediation task that was fixed is the highest-quality test case available
    - real, specific, and already proven able to break this platform. Promoting
    it means the next model change is tested against something that actually
    went wrong rather than something somebody imagined.

    PromotedCaseId is the golden case id it became. Nullable and rarely set:
    most tasks are not worth a permanent case, and promoting every one produces
    a suite too slow to run and a gate people start skipping.
*/
IF OBJECT_ID('sad.RemediationTask') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.columns
                   WHERE object_id = OBJECT_ID('sad.RemediationTask')
                     AND name = 'PromotedCaseId')
BEGIN
    ALTER TABLE sad.RemediationTask ADD PromotedCaseId VARCHAR(80) NULL;
    PRINT 'added sad.RemediationTask.PromotedCaseId';
END
ELSE PRINT 'PromotedCaseId already present or RemediationTask absent - skipped';
GO

PRINT '--- migration 022 complete ---';
GO
