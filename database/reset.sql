/*  Reset - drop every object in schema [sad], and nothing outside it.

    WHY THIS IS GENERATED RATHER THAN LISTED
    ----------------------------------------
    This file used to be a hand-ordered list of DROP TABLE statements in strict
    reverse-dependency order. That is correct exactly until someone adds a table,
    and then it fails in a way that reads like a permissions problem:

        Could not drop object 'sad.Employee' because it is referenced by a
        FOREIGN KEY constraint.

    Migration 008 added sad.ConfigurationItem, which references Employee and
    SupportGroup, and is referenced in turn by ClusterNode, CmdbApplication,
    InfrastructureCluster and Neighborhood. Nine more tables followed it across
    009 to 011. The hand-ordered list knew about none of them, so a reset left the
    schema half-dropped, the schema script then failed on objects that still
    existed, and the first visible error was four steps downstream of the cause.

    The ordering problem is real but it is not interesting, and a list that has to
    be re-derived by hand on every migration will be wrong again. So: drop every
    foreign key in the schema first, which makes the table order irrelevant, then
    drop the tables. Nothing to maintain and nothing to forget.

    SCOPE IS STILL EXACTLY [sad]. Both cursors filter on the schema name, so a
    shared database keeps everything outside it - which is the property the
    original file was protecting and the reason it is worth being careful here.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

SET NOCOUNT ON;

IF SCHEMA_ID('sad') IS NULL
BEGIN
    PRINT 'schema [sad] does not exist - nothing to reset';
    RETURN;
END

DECLARE @sql NVARCHAR(MAX);

-- 1. Every foreign key in [sad]. Once these are gone the drop order below does
--    not matter, which is the whole point.
SET @sql = N'';
SELECT @sql = @sql + N'ALTER TABLE ' + QUOTENAME(s.name) + N'.' + QUOTENAME(t.name)
                   + N' DROP CONSTRAINT ' + QUOTENAME(fk.name) + N';' + CHAR(10)
FROM   sys.foreign_keys fk
JOIN   sys.tables  t ON t.object_id = fk.parent_object_id
JOIN   sys.schemas s ON s.schema_id = t.schema_id
WHERE  s.name = 'sad';

IF LEN(ISNULL(@sql, N'')) > 0
BEGIN
    EXEC sp_executesql @sql;
    PRINT 'dropped foreign keys in [sad]';
END

-- 2. Views before tables: a view over a dropped table is not an error at drop
--    time, but leaving it behind means the schema script fails re-creating it.
SET @sql = N'';
SELECT @sql = @sql + N'DROP VIEW ' + QUOTENAME(s.name) + N'.' + QUOTENAME(v.name) + N';' + CHAR(10)
FROM   sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id
WHERE  s.name = 'sad';
IF LEN(ISNULL(@sql, N'')) > 0 EXEC sp_executesql @sql;

-- 3. The tables themselves.
SET @sql = N'';
SELECT @sql = @sql + N'DROP TABLE ' + QUOTENAME(s.name) + N'.' + QUOTENAME(t.name) + N';' + CHAR(10)
FROM   sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE  s.name = 'sad';

IF LEN(ISNULL(@sql, N'')) > 0
BEGIN
    EXEC sp_executesql @sql;
END

DECLARE @left INT = (SELECT COUNT(*) FROM sys.tables t
                     JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = 'sad');
PRINT CONCAT('reset complete - tables remaining in [sad]: ', @left);

-- Anything left is a bug in this script, not a transient condition, so it fails
-- loudly here rather than surfacing as a confusing error inside schema.sql.
IF @left > 0
    RAISERROR('reset left tables behind in schema [sad]', 16, 1);
GO
