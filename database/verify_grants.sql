/*  Does the application login hold INSERT on every table it writes to?

    WHY THIS EXISTS

    A missing grant on this platform is SILENT. Every write path deliberately
    swallows its exception - a verdict is a comment on work already delivered, so
    failing to store one must never fail the thing it was commenting on. Correct
    design, and it means a table can exist, be written to on every request, and
    stay permanently empty while the platform reports itself healthy.

    That has now happened three times:

        sad.AnswerEvaluation   018 shipped without its grant. The table existed,
                               the writer ran on every answer, and nothing was
                               stored for hours. Found only because Praveen asked
                               where the scores were.
        sad.EvalRun            020 - same
        sad.RemediationTask    021 - same

    And the cause is structural rather than careless. GRANT SELECT is issued on
    the whole schema; INSERT is issued PER TABLE, which is right for least
    privilege and means every new writable table needs a line somewhere. Worse,
    there are two "somewheres": the repository has docker/db-init.sh, production
    runs ~/infra/provision-databases.sh, and the two have diverged because
    different people patched different files.

    So this script does not grant anything. It ASKS, against the live database,
    whether the login can write where the code writes - and returns a non-empty
    result if it cannot, which a deploy step can turn into a failure.

    THE LIST IS EXPLICIT, NOT DERIVED

    Deriving it from the code would be cleverer and worse. sys.tables cannot tell
    a table the app writes from one it only reads, and a wrong answer here fails
    in the direction of granting too much. So the list is written down, and the
    cost of that is that adding a writable table means adding a line here - which
    is precisely the discipline whose absence caused the three failures above.

    Kept in sync by the deploy: if a new repository writes to a table absent from
    this list, the grant will be missing in production and this check will not
    catch it. That gap is real. It is narrower than the one it replaces, and the
    only closure is a convention: a migration that creates a writable table
    updates this file in the same commit.
*/

SET NOCOUNT ON;
GO

DECLARE @login SYSNAME = 'sad_app';

--  Every table the repository layer issues INSERT, UPDATE or DELETE against,
--  taken from app/repositories/*.py. Reads are covered by the schema-wide
--  GRANT SELECT and are deliberately not listed.
DECLARE @writable TABLE (TableName SYSNAME PRIMARY KEY);
INSERT INTO @writable (TableName) VALUES
    ('AgentAuditLog'),
    ('AnswerEvaluation'),
    ('AnswerFeedback'),
    ('CallEvaluation'),
    ('CapacityRequest'),
    ('Conversation'),
    ('ConversationTurn'),
    ('Employee'),
    ('EvalCaseResult'),
    ('EvalRun'),
    ('IndexRun'),
    ('IndexWatermark'),
    ('InfrastructureRecommendation'),
    ('Investigation'),
    ('LlmRoleOverride'),
    ('RecommendationDecision'),
    ('RemediationTask');

--  A DEVELOPER BOX HAS NO sad_app. Local development connects with integrated
--  security as the developer, so the application login legitimately does not
--  exist there. Erroring would make this script unrunnable exactly where people
--  run things by hand, and someone would delete it from the deploy.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = @login)
BEGIN
    PRINT 'no application login (' + @login + ') in this database - nothing to check';
    RETURN;
END

--  EXECUTE AS, so the answer is about the APPLICATION login rather than about
--  sa - which holds everything and would report a clean bill on a broken
--  database. This is the whole point: ask the principal that actually writes.
EXECUTE AS USER = 'sad_app';

SELECT
    w.TableName,
    CASE
        WHEN OBJECT_ID('sad.' + w.TableName) IS NULL THEN 'TABLE MISSING - migration not applied'
        ELSE 'NO INSERT - grant missing'
    END AS Problem
FROM @writable w
WHERE OBJECT_ID('sad.' + w.TableName) IS NULL
   OR HAS_PERMS_BY_NAME('sad.' + w.TableName, 'OBJECT', 'INSERT') = 0
ORDER BY w.TableName;

REVERT;
GO
