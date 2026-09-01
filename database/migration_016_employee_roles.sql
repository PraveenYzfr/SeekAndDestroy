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

-- The two must agree. require_admin reads IsAdmin, so a row claiming
-- Role='Administrator' with IsAdmin=0 would look like an administrator in every
-- report and be refused at the door - a permission bug that is invisible until
-- somebody is denied access they appear to have.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_Employee_RoleMatchesIsAdmin')
BEGIN
    ALTER TABLE sad.Employee WITH CHECK ADD CONSTRAINT CK_Employee_RoleMatchesIsAdmin
        CHECK ((Role = 'Administrator' AND IsAdmin = 1)
            OR (Role <> 'Administrator' AND IsAdmin = 0)
            OR Role IS NULL);
    PRINT 'added CK_Employee_RoleMatchesIsAdmin';
END
GO

-- E1001 is the account Praveen actually uses. Made an Approver so the approval
-- path is exercisable rather than theoretically enforced - a permission nobody
-- holds is a permission nobody has tested.
UPDATE sad.Employee SET Role = 'Approver' WHERE EmployeeNumber = 'E1001' AND IsAdmin = 0;
UPDATE sad.Employee SET Role = 'Administrator' WHERE EmployeeNumber = 'E1001' AND IsAdmin = 1;
GO

DECLARE @by_role NVARCHAR(400) = (
    SELECT STRING_AGG(CONCAT(Role, '=', cnt), ', ')
    FROM (SELECT Role, COUNT(*) AS cnt FROM sad.Employee GROUP BY Role) t);
PRINT CONCAT('employees by role: ', ISNULL(@by_role, 'none'));
GO

PRINT '--- migration 016 complete ---';
GO
