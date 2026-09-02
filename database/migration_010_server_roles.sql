/*  Migration 010 - the roles a bank's servers actually have.

    WHAT WAS WRONG
    --------------
    CK_CiServer_Role from 009 enumerated twenty roles and every one of them was a
    shared service: DNS, PKI, backup media, log collectors. That is a real part of
    an estate and it is not most of it.

    The estate had 2,007 cluster-member hosts and 2,700 infrastructure servers,
    which said that a bank runs almost everything on clustered virtualisation. It
    does not. It runs thousands of standalone servers that belong to no cluster:
    physical database hosts too large or too licensed to virtualise, batch farms,
    integration and middleware tiers, web front ends, ETL and reporting boxes.
    None of them appear in any application's hosting record and none of them fit
    the twenty roles 009 allowed.

    Rebuilt rather than extended because T-SQL has no ALTER CONSTRAINT. The guard
    makes it safe to repeat.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_CiServer_Role')
BEGIN
    ALTER TABLE sad.CiServer DROP CONSTRAINT CK_CiServer_Role;
END
GO

-- WITH NOCHECK, deliberately.
--
-- Three migrations redefine this constraint - 008 creates it, 009 and 011 widen
-- it - and each does so by dropping and re-adding, because T-SQL has no ALTER
-- CONSTRAINT. On a fresh sequential run that is fine. On a RE-RUN it is not: this
-- migration re-adds a narrower list over rows a later migration made valid, and
-- WITH CHECK validates every existing row against it. 011 adds
-- cmdb_ci_cluster_node and 012 adds Hypervisor, so re-running 009 or 010 failed
-- against data that is entirely correct.
--
-- Re-runnability is not optional here. Migrations execute BEFORE the seed on a
-- clean database, so any migration that UPDATEs seeded rows acts on an empty table
-- and silently does nothing - which is exactly how E1001 lost the administrator
-- grant that migration 006 exists to give it. The fix is to run the whole set
-- again after the seed, and that only works if every migration tolerates a second
-- run.
--
-- NOCHECK skips validation of existing rows. The constraint still governs
-- everything inserted afterwards, and the later migration that widens it runs
-- moments later in the same sequence, so the transiently-narrow window is closed
-- before anything can write through it.
ALTER TABLE sad.CiServer WITH NOCHECK ADD CONSTRAINT CK_CiServer_Role
CHECK (ServerRole IN (
    -- authentication and directory
    'DomainController', 'LDAP', 'IAM', 'PKI', 'RADIUS', 'MFA',
    -- shared services
    'DNS', 'NTP', 'SMTPRelay', 'FileServer', 'PrintServer', 'JumpHost',
    'ArtifactRepo', 'ConfigMgmt', 'Proxy',
    -- storage and protection
    'StorageController', 'BackupMedia', 'TapeLibrary',
    -- observability
    'Monitoring', 'LogCollector', 'SIEM',
    -- messaging and integration
    'MessageBroker', 'Middleware', 'IntegrationServer', 'ApiGateway',
    -- added by 010: the standalone workload tiers that are most of a real estate
    'DatabaseServer', 'AppServer', 'WebServer', 'BatchServer', 'ReportingServer',
    'EtlServer', 'CacheServer', 'SearchServer'));
PRINT 'CK_CiServer_Role extended to 33 roles';
GO

-- Reporting on role without an index means scanning every server row for a
-- question the CMDB health screen asks on every load.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CiServer_RoleZone')
BEGIN
    CREATE INDEX IX_CiServer_RoleZone ON sad.CiServer (ServerRole, NeighborhoodId) INCLUDE (HostName);
    PRINT 'created IX_CiServer_RoleZone';
END
GO

PRINT '--- migration 010 complete ---';
GO
