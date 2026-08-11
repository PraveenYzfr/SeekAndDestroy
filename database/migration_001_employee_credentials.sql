/* =============================================================================
   Migration 001 - Employee credentials for username/password sign-in.

   Idempotent: safe to run repeatedly, and safe to run against a database
   created from a schema.sql that already contains these columns (fresh
   installs get them from schema.sql; existing databases get them from here).

   Design notes:

   - PasswordHash stores a self-describing scrypt string, never a password and
     never a reversible encoding:
         scrypt$n=16384,r=8,p=1$<base64 salt>$<base64 derived key>
     The parameters live inside the value so they can be raised later without
     invalidating existing hashes - app/security/passwords.py reads them back
     out per row rather than assuming today's constants.

   - NULL PasswordHash means "no password set", which is not the same as "wrong
     password". Those employees simply cannot sign in this way; there is no
     implicit default credential anywhere in this platform.

   - No lockout/attempt columns. Rate limiting belongs at the edge (gateway or
     reverse proxy), not in a CMDB table, and pretending otherwise would give
     a false sense of protection.

   Run:  sqlcmd -S <server> -d PraveenDB -E -C -i database\migration_001_employee_credentials.sql
============================================================================= */

SET NOCOUNT ON;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sad.Employee') AND name = 'PasswordHash'
)
BEGIN
    ALTER TABLE sad.Employee ADD PasswordHash NVARCHAR(512) NULL;
    PRINT 'added sad.Employee.PasswordHash';
END
ELSE
    PRINT 'sad.Employee.PasswordHash already present - skipped';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sad.Employee') AND name = 'PasswordUpdatedAt'
)
BEGIN
    ALTER TABLE sad.Employee ADD PasswordUpdatedAt DATETIME2(3) NULL;
    PRINT 'added sad.Employee.PasswordUpdatedAt';
END
ELSE
    PRINT 'sad.Employee.PasswordUpdatedAt already present - skipped';
GO

PRINT 'migration 001 complete';
GO
