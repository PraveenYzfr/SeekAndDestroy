-- Carry the operator's model choices across a re-seed.
--
-- WHY THIS EXISTS
-- ---------------
-- reset.sql drops EVERY table in [sad] - it builds the DROP list dynamically
-- from sys.tables rather than naming them - so sad.LlmRoleOverride goes with
-- everything else. Every model assignment made on the Model Settings screen is
-- lost on any deploy-prod.sh run, and the platform silently falls back to the
-- configured defaults.
--
-- That is exactly the shape of the credential problem next door: something a
-- person set BY HAND against production, destroyed by a re-seed, with no error
-- and no sign it happened. The person only finds out later, by noticing the
-- behaviour changed.
--
-- Being asked to set the same models again after every deployment is not a
-- workflow, it is a bug with a manual workaround.
--
-- THE BACKUP TABLE LIVES IN dbo, NOT sad, for the same reason CredentialCarry
-- does: anything parked in [sad] to survive the reset would be dropped by the
-- thing it was meant to survive.
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;

IF OBJECT_ID('dbo.RoleOverrideCarry', 'U') IS NOT NULL DROP TABLE dbo.RoleOverrideCarry;

-- Guarded: on a database that predates migration 006 the table does not exist
-- yet, and a preserve step that fails would abort a deploy over the absence of
-- something optional.
IF OBJECT_ID('sad.LlmRoleOverride', 'U') IS NOT NULL
BEGIN
    SELECT RoleName, Provider, Model, UpdatedBy, UpdatedAt
    INTO   dbo.RoleOverrideCarry
    FROM   sad.LlmRoleOverride;

    SELECT CONCAT('carried ', COUNT(*), ' model assignment(s)') FROM dbo.RoleOverrideCarry;
END
ELSE
BEGIN
    -- An empty carry table rather than none, so restore_settings.sql can tell
    -- "nothing to restore" from "the preserve step never ran" - which is the
    -- distinction that makes a missing restore detectable.
    CREATE TABLE dbo.RoleOverrideCarry
    (
        RoleName  VARCHAR(60)   NOT NULL,
        Provider  VARCHAR(40)   NOT NULL,
        Model     NVARCHAR(200) NOT NULL,
        UpdatedBy NVARCHAR(100)     NULL,
        UpdatedAt DATETIME2(3)      NULL
    );
    SELECT 'sad.LlmRoleOverride not present - nothing to carry';
END
