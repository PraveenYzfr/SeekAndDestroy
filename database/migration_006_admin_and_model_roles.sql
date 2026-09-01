/*  Migration 006 - an admin flag, and per-role model selection.

    Two things, together because one is meaningless without the other: the
    screen that chooses models is the first thing in this platform that not
    every authenticated employee should be able to reach.

    WHY AN ISADMIN COLUMN AND NOT A ROLE TABLE
    ------------------------------------------
    There is exactly one privilege to express - "may change which models run" -
    and one person to grant it to. A Role/Permission/EmployeeRole triple would
    be three tables and a join to answer a question a bit already answers. When
    a second privilege appears that genuinely differs from the first, that is
    the moment to normalise; doing it now would be modelling a requirement
    nobody has stated.

    WHY ROLE OVERRIDES ARE A TABLE AND NOT CONFIG
    ---------------------------------------------
    config/settings.py is the committed default and is baked into the image, so
    changing a model there means a redeploy - which is exactly what makes
    comparing two models tedious enough that nobody does it. These rows are the
    exception to that default, editable at runtime, and they survive a redeploy
    because they are not in the image.

    A NULL row is not the same as an absent row: absent means "use the
    configured default", and that distinction is what the Reset control writes.

    ONE ROW PER ROLE, RESOLVED AT RUN START
    ---------------------------------------
    Resolution happens once when an investigation begins, not per call. A single
    run that started on DeepSeek and finished on Gemini because someone changed a
    dropdown mid-run would produce a report whose parts disagree, and
    scripts/evaluate.py - which grades recorded calls per model - would score a
    run that never actually happened as a unit.

    Idempotent, like migrations 001-005: re-running is a no-op.
*/

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sad.Employee') AND name = 'IsAdmin'
)
BEGIN
    ALTER TABLE sad.Employee ADD IsAdmin BIT NOT NULL CONSTRAINT DF_Employee_IsAdmin DEFAULT (0);
    PRINT 'added sad.Employee.IsAdmin';
END
ELSE
    PRINT 'sad.Employee.IsAdmin already present - skipped';
GO

-- E1001 is the platform owner and the only account that administers it today.
-- Written as an UPDATE rather than a seed row: the employee already exists, and
-- a migration that inserted people would fight the seed data.
UPDATE sad.Employee SET IsAdmin = 1 WHERE EmployeeNumber = 'E1001';
PRINT CONCAT('granted admin to E1001: ', @@ROWCOUNT, ' row(s)');
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.LlmRoleOverride'))
BEGIN
    CREATE TABLE sad.LlmRoleOverride
    (
        -- planning | extraction | narration | summarization | grounded_qa | reporting
        -- Not a CHECK constraint: the role list belongs to the code that calls
        -- the models, and a constraint here would mean a migration every time a
        -- chain is added. The API validates against the live list instead.
        RoleName    NVARCHAR(40)  NOT NULL,
        Provider    NVARCHAR(40)  NOT NULL,
        Model       NVARCHAR(200) NOT NULL,
        -- Who changed it and when. Switching a hot role to an expensive model
        -- has a bill attached, so an unattributable change is not acceptable -
        -- same reasoning as TriggeredBy on sad.IndexRun.
        UpdatedBy   NVARCHAR(50)      NULL,
        UpdatedAt   DATETIME2(3)  NOT NULL,
        CONSTRAINT PK_LlmRoleOverride PRIMARY KEY CLUSTERED (RoleName)
    );
    PRINT 'created sad.LlmRoleOverride';
END
ELSE
    PRINT 'sad.LlmRoleOverride already present - skipped';
GO

PRINT 'migration 006 complete';
GO
