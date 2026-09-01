-- Put the credentials back after the re-seed, then drop the carry table.
--
-- Matched on EmployeeNumber rather than EmployeeId: the seed assigns ids and a
-- regenerated corpus is free to renumber, so joining on the surrogate key would
-- silently restore Praveen's password onto whoever now holds id 1.
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;

IF OBJECT_ID('dbo.CredentialCarry', 'U') IS NULL
BEGIN
    RAISERROR('dbo.CredentialCarry is missing - the backup step did not run', 16, 1);
END
ELSE
BEGIN
    UPDATE e
       SET e.PasswordHash = c.PasswordHash
      FROM sad.Employee e
      JOIN dbo.CredentialCarry c ON c.EmployeeNumber = e.EmployeeNumber;

    SELECT CONCAT('restored ', COUNT(*), ' credential(s)')
      FROM sad.Employee e
      JOIN dbo.CredentialCarry c ON c.EmployeeNumber = e.EmployeeNumber
     WHERE e.PasswordHash IS NOT NULL;

    -- Only dropped once the restore above succeeded, so a failure part-way
    -- leaves the hashes recoverable instead of gone.
    DROP TABLE dbo.CredentialCarry;
END
