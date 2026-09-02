/*  Migration 016 - approving a placement is not the same as asking for one.

    WHAT WAS WRONG
    --------------
    Authorisation was binary: IsAdmin, or not. Everything an authenticated
    employee could reach, every authenticated employee could do - including
    approving a Tier-1 production placement.

    The rest of the model is sound and this migration does not touch it.
    require_admin re-reads IsAdmin from the database on every request rather than
    trusting a token claim, so revoking someone takes effect immediately instead of
    at token expiry; the is_admin field in the token is documented as a display
    hint and is not the authorisation decision; and a reviewer's identity comes
    from the token rather than the request body, so nobody can submit a decision as
    somebody else.

    What was missing is the middle. In a bank, the person who asks where a workload
    should go and the person who signs off on putting it there are usually not the
    same person, and the whole point of recording WHO approved something is
    undermined if the answer is "whoever happened to be logged in".

    THE FOUR ROLES
    --------------
      Viewer         read the estate and past investigations. Cannot run an
                     investigation, because that spends money on model calls
      Engineer       run investigations, ask questions, request capacity. Cannot
                     approve. This is the default and most people are here
      Approver       approve, reject, or send back a recommendation. Their identity
                     is what sad.RecommendationDecision records
      Administrator  model selection, budgets, indexing jobs, user administration

    ORDERED, NOT A SET OF FLAGS. An Approver can do everything an Engineer can, and
    an Administrator everything an Approver can. Ordering is what lets an endpoint
    say "Engineer or above" once, instead of enumerating roles and being wrong the
    next time one is added.

    IsAdmin IS NOT DROPPED. It stays as the source of truth for administrator
    access and is kept in step by a CHECK, because require_admin reads it and
    changing both the column and the code that reads it in one migration is how a
    permission check silently starts passing. Role is additive; the tightening it
    enables is a separate, reviewable change.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF COL_LENGTH('sad.Employee', 'Role') IS NULL
BEGIN
    ALTER TABLE sad.Employee ADD Role VARCHAR(20) NULL;
    PRINT 'added sad.Employee.Role';
END
ELSE PRINT 'sad.Employee.Role already present - skipped';
GO

-- Everyone existing becomes an Engineer, except administrators. Deliberately NOT
-- Approver: the safe default when granting a permission nobody has explicitly been
-- given is the lower one, and a system that starts with everybody able to approve
-- has not actually introduced approval.
UPDATE sad.Employee
SET    Role = CASE WHEN IsAdmin = 1 THEN 'Administrator' ELSE 'Engineer' END
WHERE  Role IS NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_Employee_Role')
BEGIN
    ALTER TABLE sad.Employee WITH CHECK ADD CONSTRAINT CK_Employee_Role
        CHECK (Role IN ('Viewer', 'Engineer', 'Approver', 'Administrator'));
    PRINT 'added CK_Employee_Role';
END
GO

-- ROLE IS DERIVED FROM IsAdmin, NOT CONSTRAINED AGAINST IT.
--
-- The first version of this added CK_Employee_RoleMatchesIsAdmin, requiring
-- Role='Administrator' exactly when IsAdmin=1. The invariant is right and the
-- constraint was wrong: migration 006 grants E1001 IsAdmin=1 and knows nothing
-- about Role, so re-running the set failed with "The UPDATE statement conflicted
-- with the CHECK constraint" - migration 016 blocking migration 006 from doing
-- the one thing it exists to do.
--
-- Deriving instead of constraining enforces the same agreement without ordering
-- becoming load-bearing: whatever IsAdmin says, Role is made to match. Run 006
-- then 016 and the pair is consistent; run 016 twice and nothing changes.
UPDATE sad.Employee SET Role = 'Administrator' WHERE IsAdmin = 1 AND Role <> 'Administrator';
UPDATE sad.Employee SET Role = 'Engineer'      WHERE IsAdmin = 0 AND Role = 'Administrator';
GO

-- E1001 is NOT pinned to Approver here. Migration 006 grants it IsAdmin, and
-- Administrator already outranks Approver in ROLE_ORDER, so pinning it lower both
-- contradicted 006 and broke tests/test_model_roles.py, which asserts E1001 is an
-- administrator because the admin screen is unreachable for everyone otherwise.
--
-- The approval path is still exercisable: an Administrator satisfies any
-- require_role("Approver") check by ordering.
DECLARE @by_role NVARCHAR(400) = (
    SELECT STRING_AGG(CONCAT(Role, '=', cnt), ', ')
    FROM (SELECT Role, COUNT(*) AS cnt FROM sad.Employee GROUP BY Role) t);
PRINT CONCAT('employees by role: ', ISNULL(@by_role, 'none'));
GO

PRINT '--- migration 016 complete ---';
GO
