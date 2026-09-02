/*  Migration 009 - the infrastructure an estate actually contains.

    WHAT WAS MISSING
    ----------------
    After 008 the CMDB held applications, clusters and the hosts that run them,
    and nothing else. No authentication tier, no shared services, no storage, no
    network. Every server in the estate existed to host an application, which is
    not what a bank's floor looks like - most of the boxes are DNS, domain
    controllers, PKI, backup media servers, log collectors and jump hosts, and
    none of them appear in a single application's hosting record.

    It also had no VM layer, so "four VMs landing on two physical hosts" was not
    expressible and ResiliencyScore could only ever count nodes.

    WHY STORAGE IS THE INTERESTING PART
    -----------------------------------
    Four VMs on two hosts is a finding. Four VMs whose datastores all come off one
    NAS head is a bigger one, and it is invisible without modelling the array and
    the volume separately. Relationship type 6, Provides::Uses, is what turns
    "which workloads die if atl-nas-04 fails" into one traversal instead of an
    unanswerable question.

    TWO CLASS TABLES FOR SERVERS, DELIBERATELY
    ------------------------------------------
    sad.ClusterNode.ClusterId is NOT NULL and the scoring path relies on every
    node having a cluster. Rather than weaken that to accommodate a domain
    controller, infrastructure servers get their own class table. The two sets are
    disjoint - a host is either a member of an application cluster or a standalone
    infrastructure server - so nothing is duplicated and neither table needs to
    know about the other. Both produce cmdb_ci_server configuration items.

    ZONES ARE TYPED
    ---------------
    Storage, authentication, network and management each get their own zone per
    site rather than being mixed into application compute. That is how a floor is
    actually laid out, and it means a zone-level question ("what is in CORE-01")
    returns something coherent. Every class is spread evenly across all eight data
    centres so no function has a single-site failure domain - 60 authentication
    servers per site rather than 480 in one.

    IDEMPOTENT by guard throughout.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

-- =============================================================================
-- 1. New CI classes
-- =============================================================================
-- The CHECK from 008 allowed nine classes. Dropped and rebuilt rather than
-- extended in place because T-SQL has no ALTER CONSTRAINT; the guard makes the
-- rebuild safe to repeat.
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_ConfigurationItem_Class')
BEGIN
    ALTER TABLE sad.ConfigurationItem DROP CONSTRAINT CK_ConfigurationItem_Class;
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
ALTER TABLE sad.ConfigurationItem WITH NOCHECK ADD CONSTRAINT CK_ConfigurationItem_Class
CHECK (ClassName IN (
    'cmdb_ci_appl', 'cmdb_ci_service', 'cmdb_ci_cluster', 'cmdb_ci_server',
    'cmdb_ci_vm_instance', 'cmdb_ci_db_instance', 'cmdb_ci_lb',
    'cmdb_ci_datacenter', 'cmdb_ci_zone',
    -- added by 009
    'cmdb_ci_netgear', 'cmdb_ci_storage_array', 'cmdb_ci_storage_volume',
    'cmdb_ci_msg_queue'));
PRINT 'CK_ConfigurationItem_Class extended to 13 classes';
GO

-- =============================================================================
-- 2. Provides::Uses
-- =============================================================================
-- Not containment. An array provides a volume and a volume is used by many hosts,
-- so the same volume legitimately appears under several parents' subtrees - which
-- is exactly what makes it a shared failure domain worth reporting. Marking it
-- containment would assert a tree that does not exist.
IF NOT EXISTS (SELECT 1 FROM sad.CiRelationshipType WHERE TypeId = 6)
BEGIN
    INSERT INTO sad.CiRelationshipType (TypeId, Name, ParentDescriptor, ChildDescriptor, IsContainment)
    VALUES (6, 'Provides::Uses', 'Provides', 'Uses', 0);
    PRINT 'added relationship type 6 Provides::Uses';
END
ELSE PRINT 'relationship type 6 already present - skipped';
GO

-- =============================================================================
-- 3. Zones carry a type
-- =============================================================================
-- Enforced in the schema rather than implied by the zone's name, because a
-- convention that lives only in a naming pattern is a convention nothing checks.
IF COL_LENGTH('sad.Neighborhood', 'ZoneType') IS NULL
BEGIN
    ALTER TABLE sad.Neighborhood ADD ZoneType VARCHAR(20) NULL;
    PRINT 'added sad.Neighborhood.ZoneType';
END
ELSE PRINT 'sad.Neighborhood.ZoneType already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_Neighborhood_ZoneType')
BEGIN
    ALTER TABLE sad.Neighborhood WITH NOCHECK ADD CONSTRAINT CK_Neighborhood_ZoneType
        CHECK (ZoneType IS NULL OR ZoneType IN ('Compute','Storage','Core','Network','Management'));
    PRINT 'added CK_Neighborhood_ZoneType';
END
GO

-- Existing zones predate the type and are all application compute.
UPDATE sad.Neighborhood SET ZoneType = 'Compute' WHERE ZoneType IS NULL;
GO

-- =============================================================================
-- 4. Servers that are not cluster members
-- =============================================================================
-- Domain controllers, DNS, PKI, backup media servers, log collectors, jump hosts.
-- Disjoint from sad.ClusterNode by construction: a host is either in an
-- application cluster or it is one of these.
IF OBJECT_ID('sad.CiServer', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiServer
    (
        CiId            INT           NOT NULL,
        HostName        VARCHAR(255)  NOT NULL,
        ServerRole      VARCHAR(40)   NOT NULL,
        NeighborhoodId  INT           NOT NULL,
        IpAddress       VARCHAR(45)   NULL,
        CpuCores        INT           NOT NULL,
        MemoryGb        INT           NOT NULL,
        StorageGb       INT           NOT NULL,
        OperatingSystem VARCHAR(60)   NULL,
        IsVirtual       BIT           NOT NULL CONSTRAINT DF_CiServer_IsVirtual DEFAULT (0),
        LastSeenAt      DATETIME2(3)  NULL,
        CONSTRAINT PK_CiServer PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiServer_Ci   FOREIGN KEY (CiId)           REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiServer_Zone FOREIGN KEY (NeighborhoodId) REFERENCES sad.Neighborhood (NeighborhoodId),
        CONSTRAINT UQ_CiServer_HostName UNIQUE (HostName),
        CONSTRAINT CK_CiServer_Role CHECK (ServerRole IN (
            'DomainController','LDAP','IAM','PKI','RADIUS','MFA',
            'DNS','NTP','SMTPRelay','FileServer','JumpHost','ArtifactRepo','ConfigMgmt',
            'StorageController','BackupMedia',
            'Monitoring','LogCollector','SIEM',
            'MessageBroker','Middleware'))
    );
    CREATE INDEX IX_CiServer_Role ON sad.CiServer (ServerRole);
    CREATE INDEX IX_CiServer_Zone ON sad.CiServer (NeighborhoodId);
    PRINT 'created sad.CiServer';
END
ELSE PRINT 'sad.CiServer already present - skipped';
GO

-- Cluster members gain a role too: a hypervisor and a bare-metal database host
-- are both cluster nodes and are not interchangeable when placing a workload.
IF COL_LENGTH('sad.ClusterNode', 'ServerRole') IS NULL
BEGIN
    ALTER TABLE sad.ClusterNode ADD
        ServerRole   VARCHAR(40) NULL,
        IsHypervisor BIT NOT NULL CONSTRAINT DF_ClusterNode_IsHypervisor DEFAULT (0);
    PRINT 'added sad.ClusterNode.ServerRole / IsHypervisor';
END
ELSE PRINT 'sad.ClusterNode role columns already present - skipped';
GO

UPDATE sad.ClusterNode SET ServerRole = 'Hypervisor', IsHypervisor = 1 WHERE ServerRole IS NULL;
GO

-- =============================================================================
-- 5. Storage
-- =============================================================================
IF OBJECT_ID('sad.CiStorageArray', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiStorageArray
    (
        CiId             INT           NOT NULL,
        ArrayName        VARCHAR(120)  NOT NULL,
        Vendor           VARCHAR(60)   NULL,
        ArrayType        VARCHAR(20)   NOT NULL,   -- NAS | SAN | Object
        Protocol         VARCHAR(20)   NOT NULL,   -- NFS | SMB | FC | iSCSI | S3
        RawCapacityTb    DECIMAL(10,2) NOT NULL,
        UsableCapacityTb DECIMAL(10,2) NOT NULL,
        UsedTb           DECIMAL(10,2) NOT NULL,
        ControllerCount  INT           NOT NULL CONSTRAINT DF_CiStorageArray_Controllers DEFAULT (2),
        NeighborhoodId   INT           NOT NULL,
        CONSTRAINT PK_CiStorageArray PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiStorageArray_Ci   FOREIGN KEY (CiId)           REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiStorageArray_Zone FOREIGN KEY (NeighborhoodId) REFERENCES sad.Neighborhood (NeighborhoodId),
        CONSTRAINT UQ_CiStorageArray_Name UNIQUE (ArrayName),
        CONSTRAINT CK_CiStorageArray_Type     CHECK (ArrayType IN ('NAS','SAN','Object')),
        CONSTRAINT CK_CiStorageArray_Protocol CHECK (Protocol IN ('NFS','SMB','FC','iSCSI','S3'))
    );
    PRINT 'created sad.CiStorageArray';
END
ELSE PRINT 'sad.CiStorageArray already present - skipped';
GO

-- The volume is the shared failure domain, not the array. Two clusters that are
-- independent on paper and mount the same export are not independent, and that is
-- the finding this table exists to make computable.
IF OBJECT_ID('sad.CiStorageVolume', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiStorageVolume
    (
        CiId           INT           NOT NULL,
        VolumeName     VARCHAR(120)  NOT NULL,
        ArrayCiId      INT           NOT NULL,
        CapacityGb     INT           NOT NULL,
        UsedGb         INT           NOT NULL,
        Protocol       VARCHAR(20)   NOT NULL,
        ExportPath     VARCHAR(255)  NULL,
        PerformanceTier VARCHAR(20)  NULL,        -- Gold | Silver | Bronze
        IsReplicated   BIT           NOT NULL CONSTRAINT DF_CiStorageVolume_Replicated DEFAULT (0),
        ReplicaDcCiId  INT           NULL,
        CONSTRAINT PK_CiStorageVolume PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiStorageVolume_Ci      FOREIGN KEY (CiId)          REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiStorageVolume_Array   FOREIGN KEY (ArrayCiId)     REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiStorageVolume_Replica FOREIGN KEY (ReplicaDcCiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiStorageVolume_Name UNIQUE (VolumeName),
        CONSTRAINT CK_CiStorageVolume_Tier CHECK (PerformanceTier IS NULL OR PerformanceTier IN ('Gold','Silver','Bronze'))
    );
    CREATE INDEX IX_CiStorageVolume_Array ON sad.CiStorageVolume (ArrayCiId);
    PRINT 'created sad.CiStorageVolume';
END
ELSE PRINT 'sad.CiStorageVolume already present - skipped';
GO

-- =============================================================================
-- 6. Network
-- =============================================================================
-- The other shared dependency that makes redundancy fake. Two hosts in separate
-- racks are still one failure if they share an aggregation switch.
IF OBJECT_ID('sad.CiNetworkDevice', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiNetworkDevice
    (
        CiId           INT           NOT NULL,
        DeviceName     VARCHAR(120)  NOT NULL,
        DeviceRole     VARCHAR(30)   NOT NULL,   -- TopOfRack | Aggregation | Core | Firewall
        Vendor         VARCHAR(60)   NULL,
        PortCount      INT           NULL,
        NeighborhoodId INT           NOT NULL,
        RedundancyPeerCiId INT       NULL,
        CONSTRAINT PK_CiNetworkDevice PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiNetworkDevice_Ci   FOREIGN KEY (CiId)               REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiNetworkDevice_Zone FOREIGN KEY (NeighborhoodId)     REFERENCES sad.Neighborhood (NeighborhoodId),
        CONSTRAINT FK_CiNetworkDevice_Peer FOREIGN KEY (RedundancyPeerCiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiNetworkDevice_Name UNIQUE (DeviceName),
        CONSTRAINT CK_CiNetworkDevice_Role CHECK (DeviceRole IN ('TopOfRack','Aggregation','Core','Firewall'))
    );
    PRINT 'created sad.CiNetworkDevice';
END
ELSE PRINT 'sad.CiNetworkDevice already present - skipped';
GO

-- =============================================================================
-- 7. Messaging
-- =============================================================================
IF OBJECT_ID('sad.CiMessageQueue', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiMessageQueue
    (
        CiId        INT           NOT NULL,
        QueueName   VARCHAR(120)  NOT NULL,
        Broker      VARCHAR(40)   NOT NULL,   -- Kafka | RabbitMQ | IBMMQ | ActiveMQ
        IsClustered BIT           NOT NULL CONSTRAINT DF_CiMessageQueue_Clustered DEFAULT (1),
        CONSTRAINT PK_CiMessageQueue PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiMessageQueue_Ci FOREIGN KEY (CiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiMessageQueue_Name UNIQUE (QueueName)
    );
    PRINT 'created sad.CiMessageQueue';
END
ELSE PRINT 'sad.CiMessageQueue already present - skipped';
GO

-- =============================================================================
-- 8. VM placement
-- =============================================================================
-- HostCiId is the physical host the VM currently runs on. It is DERIVED - the
-- authority is the Hosted on::Hosts edge - and exists because "how many distinct
-- physical parents does this application have" is asked on the scoring hot path
-- and should not require a traversal per candidate.
IF COL_LENGTH('sad.CiVmInstance', 'HostCiId') IS NULL
BEGIN
    ALTER TABLE sad.CiVmInstance ADD
        HostCiId       INT NULL CONSTRAINT FK_CiVmInstance_Host   FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId),
        VolumeCiId     INT NULL CONSTRAINT FK_CiVmInstance_Volume FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId),
        OperatingSystem VARCHAR(60) NULL;
    PRINT 'added sad.CiVmInstance.HostCiId / VolumeCiId / OperatingSystem';
END
ELSE PRINT 'sad.CiVmInstance placement columns already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CiVmInstance_Host')
   AND COL_LENGTH('sad.CiVmInstance', 'HostCiId') IS NOT NULL
BEGIN
    CREATE INDEX IX_CiVmInstance_Host   ON sad.CiVmInstance (HostCiId);
    CREATE INDEX IX_CiVmInstance_Volume ON sad.CiVmInstance (VolumeCiId);
    PRINT 'created CiVmInstance indexes';
END
GO

PRINT '--- migration 009 complete ---';
GO
