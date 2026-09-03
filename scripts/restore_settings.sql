-- Put the operator's model choices back after the re-seed, then drop the carry.
--
-- Matched on RoleName, which is the natural key - LlmRoleOverride is keyed by
-- the role, not by a surrogate id, so there is nothing here that a regenerated
-- corpus can renumber underneath us.
--
-- MERGE rather than INSERT: migration 006 may seed rows of its own, and a plain
-- insert would collide with them. The operator's choice wins over anything the
-- migration put there - that is the whole point of the table.
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;

IF OBJECT_ID('dbo.RoleOverrideCarry', 'U') IS NULL
BEGIN
    RAISERROR('dbo.RoleOverrideCarry is missing - the preserve step did not run', 16, 1);
END
ELSE IF OBJECT_ID('sad.LlmRoleOverride', 'U') IS NULL
BEGIN
    -- Migrations run before this, so the table should exist. If it does not,
    -- the carry is KEPT rather than dropped: losing the assignments silently is
    -- the exact failure this file was written to stop.
    RAISERROR('sad.LlmRoleOverride is missing after migrations - carry table kept', 16, 1);
END
ELSE
BEGIN
    MERGE sad.LlmRoleOverride AS target
    USING dbo.RoleOverrideCarry AS src
       ON target.RoleName = src.RoleName
    WHEN MATCHED THEN
        UPDATE SET Provider = src.Provider, Model = src.Model,
                   UpdatedBy = src.UpdatedBy, UpdatedAt = src.UpdatedAt
    WHEN NOT MATCHED THEN
        INSERT (RoleName, Provider, Model, UpdatedBy, UpdatedAt)
        VALUES (src.RoleName, src.Provider, src.Model, src.UpdatedBy, src.UpdatedAt);

    SELECT CONCAT('restored ', COUNT(*), ' model assignment(s)')
      FROM sad.LlmRoleOverride o
      JOIN dbo.RoleOverrideCarry c ON c.RoleName = o.RoleName;

    -- Only dropped once the restore above succeeded, so a failure part-way
    -- leaves the assignments recoverable instead of gone.
    DROP TABLE dbo.RoleOverrideCarry;
END
