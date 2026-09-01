-- Carry credentials across a re-seed.
--
-- reset.sql drops sad.Employee, and PasswordHash is a column on it. Praveen set
-- E1001's password by hand against production; re-seeding without this step
-- locks him out of sad.praveenyzfr.com with no way back in except setting it
-- again.
--
-- The backup table lives in dbo, NOT in sad, because reset.sql drops everything
-- in sad by design. Anything parked there to survive the reset would be dropped
-- by the thing it was meant to survive.
--
-- Nothing here prints a hash. The value moves table-to-table inside the server.
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;

IF OBJECT_ID('dbo.CredentialCarry', 'U') IS NOT NULL DROP TABLE dbo.CredentialCarry;

SELECT EmployeeNumber, PasswordHash
INTO   dbo.CredentialCarry
FROM   sad.Employee
WHERE  PasswordHash IS NOT NULL;

SELECT CONCAT('carried ', COUNT(*), ' credential(s)') FROM dbo.CredentialCarry;
