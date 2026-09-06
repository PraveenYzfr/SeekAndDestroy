/*  Migration 024 - WHY a fidelity score was not measured.

    THE PROBLEM
    -----------
    NumberFidelity NULL means "not measured", and thresholds.py turns that into
    a PASS:

        if rate is None:
            return Verdict(Outcome.PASS, f"{name}: not measured")

    Measured on prod, thirteen answers evaluated in three hours:

        measured and FAILING      5    rates 0.000, 0.191, 0.400, 0.410, 0.500
        not measured (auto-PASS)  8
        CLEAN PASSES              0

    Not one answer passed on merit. Every pass in that window was a pass because
    the gate could not look - and NOTHING RECORDS WHY IT COULD NOT LOOK. At least
    three unrelated situations collapse into the same NULL:

        the evidence was retrieved FREE TEXT, so a figure may have been quoted
        faithfully and this grader genuinely cannot tell     -> honest PASS

        the prose quoted NO FIGURES at all, so there was
        nothing to check                                     -> honest PASS

        the grounding set held only the INVESTIGATION ID, so
        the answer was judged measurable against one
        meaningless value                                    -> NOT honest; this
                                                                is the one that
                                                                scores 0.05

    An auto-PASS that cannot be read can only be trusted, and eight of thirteen
    is too many to trust.

    THIS IS THE SAME DEFECT AS THE JUDGE'S SINGLE no_evidence LABEL, which was
    split in a3ec422 for exactly the same reason: an infrastructure fault, a
    pipeline defect and a contract break were raising one indistinguishable
    alarm. Absent is not zero - and when something IS absent, which absence it
    was is the part that says what to do about it.

    WHY A COLUMN RATHER THAN A LOG LINE
    -----------------------------------
    The rate itself is stored, so the reason has to live beside it or a later
    reader is joining a number to a log they no longer have. NVARCHAR rather
    than a lookup table: the set will grow as graders learn new ways of being
    unable to measure, and a constraint that has to be migrated for every new
    reason is a constraint people work around by reusing a wrong one.
*/
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;
GO

IF COL_LENGTH('sad.AnswerEvaluation', 'NumberFidelityAbsence') IS NULL
BEGIN
    ALTER TABLE sad.AnswerEvaluation ADD
        --  NULL when NumberFidelity HAS a value. Non-null names which kind of
        --  nothing was found, and is the difference between "this answer had no
        --  figures to check" and "we could not see the figures it had".
        NumberFidelityAbsence NVARCHAR(40) NULL,
        --  Same, for entity fidelity. Added together because the two graders
        --  fail to apply for different reasons and a single shared column would
        --  reintroduce the conflation this migration exists to remove.
        EntityFidelityAbsence NVARCHAR(40) NULL;
    PRINT 'added AnswerEvaluation absence reasons';
END
ELSE PRINT 'AnswerEvaluation absence reasons already present - skipped';
GO
