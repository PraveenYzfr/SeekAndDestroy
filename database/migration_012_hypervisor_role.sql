/*  Migration 012 - a hypervisor is a server role.

    WHAT WAS WRONG
    --------------
    Migration 010 enumerated thirty-three server roles and every one of them was a
    workload or a shared service. It had no role for the machines that run the
    virtualisation estate, which are 2,007 of the 10,931 servers - the largest
    single population.

    The omission was invisible until data arrived. The seed inserted them with
    ServerRole = 'Hypervisor', CK_CiServer_Role rejected the batch, and because
    sqlcmd stops the script on a constraint failure, everything ordered after
    sad.CiServer never ran: 30,105 VMs and all 85,526 relationships were silently
    absent from a load that reported 54,555 configuration items and looked
    substantially successful.

    Worth recording, because it is the same shape as the seed failure earlier
    tonight: a partial load whose visible symptom is a plausible number rather
    than an error. The verification step in scripts/deploy-prod.sh checks VM and
    relationship counts specifically for this reason.

    Also adds the two roles the estate needs and 010 lacked - a machine that is a
    cluster member but not a hypervisor, and the container hosts that are neither.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_CiServer_Role')
BEGIN
    ALTER TABLE sad.CiServer DROP CONSTRAINT CK_CiServer_Role;
END
GO

ALTER TABLE sad.CiServer WITH NOCHECK ADD CONSTRAINT CK_CiServer_Role
CHECK (ServerRole IN (
    -- virtualisation: the largest population, and absent until 012
    'Hypervisor', 'ContainerHost', 'BareMetalNode',
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
    -- standalone workload tiers
    'DatabaseServer', 'AppServer', 'WebServer', 'BatchServer', 'ReportingServer',
    'EtlServer', 'CacheServer', 'SearchServer'));
PRINT 'CK_CiServer_Role extended to 36 roles including Hypervisor';
GO

PRINT '--- migration 012 complete ---';
GO
