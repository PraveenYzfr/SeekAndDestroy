/*  Migration 008 - a CMDB shaped like a CMDB.

    WHAT WAS WRONG
    --------------
    sad.Incident carried three parallel nullable foreign keys:

        ApplicationId, ClusterId, NodeId

    with CK_Incident_HasSubject requiring at least one. Convenient to query, and
    not a configuration-management database. Four specific failures:

      1. It can only express three classes. A database instance, a load balancer
         or a business service has nowhere to go, so the estate cannot contain
         one - a schema change per concept.
      2. It cannot express multiplicity. A real incident affects several CIs;
         there is one NodeId. ServiceNow has task_ci for exactly this.
      3. It cannot express the chain. Application -> VM -> host -> cluster ->
         zone -> data centre. NodeId names the box and not the path, so
         "what breaks if this host dies" is unanswerable.
      4. Nothing enforces agreement. NodeId 5 beside ClusterId 9 is accepted when
         node 5 is not in cluster 9. In a relationship model the contradiction is
         unrepresentable, because the edge IS the fact.

    (4) has a live cousin: DataCenter is an NVARCHAR duplicated on both
    Neighborhood and InfrastructureCluster, agreeing today only because one
    generator writes both.

    WHY IT MATTERS BEYOND REALISM
    -----------------------------
    ResiliencyScore is currently close to node count. An application on four VMs
    that land on two physical hosts sharing one top-of-rack switch scores as
    four-way redundant and dies with one switch. Warning about that is the
    recommendation this platform exists to make, and the old model could not
    compute it at all.

    WHAT THIS DOES
    --------------
    Follows ServiceNow's actual structure: one cmdb_ci base table with class
    inheritance, typed edges in cmdb_rel_ci, affected CIs in task_ci.

    Existing tables are NOT replaced. CmdbApplication, InfrastructureCluster,
    ClusterNode and Neighborhood each gain a CiId and BECOME their class table -
    which is what ServiceNow does, since cmdb_ci_appl is not a copy of cmdb_ci but
    the extra attributes for that class joined on the same key. No data migration,
    no duplicate truth, and the scoring path keeps working untouched.

    The old FK columns survive as DERIVED columns fed from the graph. They are an
    index for the scoring hot path and are never the source of truth; every one is
    commented as such at the point of definition.

    IDEMPOTENT by guard throughout - every object is created only if absent and
    every backfill is written NOT EXISTS, so this runs safely on a fresh database
    and over an existing one.
*/

SET QUOTED_IDENTIFIER ON;   -- required at creation AND at every later insert for
SET ANSI_NULLS ON;          -- filtered indexes; omitting it has bitten this project
GO                          -- three separate times.

-- =============================================================================
-- 1. The base CI table
-- =============================================================================
-- SysId rather than only an INT identity: a CMDB's identity has to survive a
-- reload. Row ids are assigned by insert order and change on every regeneration,
-- so anything that references a CI across systems or across a re-seed needs a key
-- the generator controls.
IF OBJECT_ID('sad.ConfigurationItem', 'U') IS NULL
BEGIN
    CREATE TABLE sad.ConfigurationItem
    (
        CiId                INT IDENTITY(1,1)  NOT NULL,
        SysId               CHAR(32)           NOT NULL,
        Name                NVARCHAR(255)      NOT NULL,
        ClassName           VARCHAR(40)        NOT NULL,
        OperationalStatus   VARCHAR(20)        NOT NULL CONSTRAINT DF_ConfigurationItem_OpStatus DEFAULT ('Operational'),
        InstallStatus       VARCHAR(20)        NOT NULL CONSTRAINT DF_ConfigurationItem_InstStatus DEFAULT ('Installed'),
        Environment         VARCHAR(20)        NULL,
        SupportGroupId      INT                NULL,
        OwnedById           INT                NULL,   -- business owner
        ManagedById         INT                NULL,   -- technical owner
        DataClassification  VARCHAR(20)        NULL,
        RegulatoryScope     VARCHAR(40)        NULL,   -- 'SOX', 'PCI', 'SOX,PCI'
        FirstDiscovered     DATETIME2(3)       NULL,
        LastDiscovered      DATETIME2(3)       NULL,   -- staleness is measured from this
        DiscoverySource     VARCHAR(30)        NULL,   -- Discovery | Manual | Import
        CreatedAt           DATETIME2(3)       NOT NULL CONSTRAINT DF_ConfigurationItem_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt           DATETIME2(3)       NOT NULL CONSTRAINT DF_ConfigurationItem_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_ConfigurationItem PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT UQ_ConfigurationItem_SysId UNIQUE (SysId),
        CONSTRAINT FK_ConfigurationItem_SupportGroup FOREIGN KEY (SupportGroupId) REFERENCES sad.SupportGroup (SupportGroupId),
        CONSTRAINT FK_ConfigurationItem_OwnedBy      FOREIGN KEY (OwnedById)      REFERENCES sad.Employee (EmployeeId),
        CONSTRAINT FK_ConfigurationItem_ManagedBy    FOREIGN KEY (ManagedById)    REFERENCES sad.Employee (EmployeeId),
        -- Class names mirror ServiceNow's sys_class_name so the schema is
        -- recognisable to anyone who administers one.
        CONSTRAINT CK_ConfigurationItem_Class CHECK (ClassName IN (
            'cmdb_ci_appl', 'cmdb_ci_service', 'cmdb_ci_cluster', 'cmdb_ci_server',
            'cmdb_ci_vm_instance', 'cmdb_ci_db_instance', 'cmdb_ci_lb',
            'cmdb_ci_datacenter', 'cmdb_ci_zone')),
        CONSTRAINT CK_ConfigurationItem_OpStatus CHECK (OperationalStatus IN
            ('Operational','NonOperational','Repair','Retired')),
        CONSTRAINT CK_ConfigurationItem_InstStatus CHECK (InstallStatus IN
            ('Installed','InMaintenance','Retired','Absent','OnOrder')),
        CONSTRAINT CK_ConfigurationItem_Classification CHECK (DataClassification IS NULL
            OR DataClassification IN ('Public','Internal','Confidential','Restricted'))
    );
    PRINT 'created sad.ConfigurationItem';
END
ELSE PRINT 'sad.ConfigurationItem already present - skipped';
GO

IF OBJECT_ID('sad.ConfigurationItem', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ConfigurationItem_Class')
BEGIN
    CREATE INDEX IX_ConfigurationItem_Class ON sad.ConfigurationItem (ClassName) INCLUDE (Name);
    CREATE INDEX IX_ConfigurationItem_Stale ON sad.ConfigurationItem (LastDiscovered);
    CREATE INDEX IX_ConfigurationItem_Name  ON sad.ConfigurationItem (Name);
    PRINT 'created ConfigurationItem indexes';
END
GO

-- =============================================================================
-- 2. Relationship types
-- =============================================================================
-- Two descriptors per type, as ServiceNow does: the same edge reads differently
-- from each end. Without both, a traversal result cannot be described in a
-- sentence without the reader guessing which way round it goes.
IF OBJECT_ID('sad.CiRelationshipType', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiRelationshipType
    (
        TypeId            INT           NOT NULL,
        Name              VARCHAR(60)   NOT NULL,   -- 'Runs on::Runs'
        ParentDescriptor  VARCHAR(40)   NOT NULL,   -- what the parent does to the child
        ChildDescriptor   VARCHAR(40)   NOT NULL,   -- what the child does to the parent
        IsContainment     BIT           NOT NULL,   -- containment must stay acyclic
        CONSTRAINT PK_CiRelationshipType PRIMARY KEY CLUSTERED (TypeId),
        CONSTRAINT UQ_CiRelationshipType_Name UNIQUE (Name)
    );
    PRINT 'created sad.CiRelationshipType';
END
ELSE PRINT 'sad.CiRelationshipType already present - skipped';
GO

-- IsContainment is not decoration. Containment edges describe physical nesting and
-- are acyclic by construction; dependency edges legitimately cycle, because two
-- applications calling each other is a real topology and not bad data. A traversal
-- that assumes the whole graph is a tree will loop, and a blast-radius query that
-- loops - or silently truncates at the default recursion ceiling - returns a number
-- that looks complete and is too small. That is a wrong-number bug, not a crash.
MERGE sad.CiRelationshipType AS target
USING (VALUES
    (1, 'Runs on::Runs',       'Runs',     'Runs on',    1),
    (2, 'Hosted on::Hosts',    'Hosts',    'Hosted on',  1),
    (3, 'Member of::Members',  'Members',  'Member of',  1),
    (4, 'Depends on::Used by', 'Used by',  'Depends on', 0),
    (5, 'Located in::Contains','Contains', 'Located in', 1)
) AS source (TypeId, Name, ParentDescriptor, ChildDescriptor, IsContainment)
ON target.TypeId = source.TypeId
WHEN NOT MATCHED THEN
    INSERT (TypeId, Name, ParentDescriptor, ChildDescriptor, IsContainment)
    VALUES (source.TypeId, source.Name, source.ParentDescriptor, source.ChildDescriptor, source.IsContainment);
PRINT 'relationship types present';
GO

-- =============================================================================
-- 3. The edges
-- =============================================================================
-- Parent is the container or the depended-upon; child is the contained or the
-- dependent. Blast radius therefore walks parent -> child: start at the failing
-- CI and recurse down. Stated here because getting it backwards produces results
-- that are plausible, inverted, and very hard to notice.
IF OBJECT_ID('sad.CiRelationship', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiRelationship
    (
        RelationshipId  INT IDENTITY(1,1) NOT NULL,
        ParentCiId      INT               NOT NULL,
        ChildCiId       INT               NOT NULL,
        TypeId          INT               NOT NULL,
        CreatedAt       DATETIME2(3)      NOT NULL CONSTRAINT DF_CiRelationship_CreatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_CiRelationship PRIMARY KEY CLUSTERED (RelationshipId),
        CONSTRAINT FK_CiRelationship_Parent FOREIGN KEY (ParentCiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiRelationship_Child  FOREIGN KEY (ChildCiId)  REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiRelationship_Type   FOREIGN KEY (TypeId)     REFERENCES sad.CiRelationshipType (TypeId),
        CONSTRAINT UQ_CiRelationship UNIQUE (ParentCiId, ChildCiId, TypeId),
        -- A CI related to itself is always bad data, whatever the type.
        CONSTRAINT CK_CiRelationship_NoSelf CHECK (ParentCiId <> ChildCiId)
    );
    CREATE INDEX IX_CiRelationship_Parent ON sad.CiRelationship (ParentCiId, TypeId) INCLUDE (ChildCiId);
    CREATE INDEX IX_CiRelationship_Child  ON sad.CiRelationship (ChildCiId, TypeId)  INCLUDE (ParentCiId);
    PRINT 'created sad.CiRelationship';
END
ELSE PRINT 'sad.CiRelationship already present - skipped';
GO

-- =============================================================================
-- 4. Affected CIs on a task
-- =============================================================================
-- ServiceNow's task_ci. An incident names one primary CI on the record and any
-- number of additional affected ones; the old model could hold exactly one link
-- per class and no notion of "also affected".
IF OBJECT_ID('sad.TaskCi', 'U') IS NULL
BEGIN
    CREATE TABLE sad.TaskCi
    (
        TaskCiId   INT IDENTITY(1,1) NOT NULL,
        TaskType   VARCHAR(20)       NOT NULL,   -- Incident | Change | Problem
        TaskId     INT               NOT NULL,
        CiId       INT               NOT NULL,
        IsPrimary  BIT               NOT NULL CONSTRAINT DF_TaskCi_IsPrimary DEFAULT (0),
        CONSTRAINT PK_TaskCi PRIMARY KEY CLUSTERED (TaskCiId),
        CONSTRAINT FK_TaskCi_Ci FOREIGN KEY (CiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_TaskCi UNIQUE (TaskType, TaskId, CiId),
        CONSTRAINT CK_TaskCi_TaskType CHECK (TaskType IN ('Incident','Change','Problem'))
    );
    CREATE INDEX IX_TaskCi_Task ON sad.TaskCi (TaskType, TaskId) INCLUDE (CiId, IsPrimary);
    CREATE INDEX IX_TaskCi_Ci   ON sad.TaskCi (CiId) INCLUDE (TaskType, TaskId);
    PRINT 'created sad.TaskCi';
END
ELSE PRINT 'sad.TaskCi already present - skipped';
GO

-- =============================================================================
-- 5. Existing tables become class tables
-- =============================================================================
-- Each gains CiId only. Cores, memory, hypervisor and cost stay exactly where they
-- are: identity and lifecycle move up to ConfigurationItem, class-specific
-- attributes stay down here. Nothing that reads these tables today has to change.
IF COL_LENGTH('sad.CmdbApplication', 'CiId') IS NULL
BEGIN
    ALTER TABLE sad.CmdbApplication ADD CiId INT NULL
        CONSTRAINT FK_CmdbApplication_Ci FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId);
    PRINT 'added sad.CmdbApplication.CiId';
END
ELSE PRINT 'sad.CmdbApplication.CiId already present - skipped';
GO

IF COL_LENGTH('sad.InfrastructureCluster', 'CiId') IS NULL
BEGIN
    ALTER TABLE sad.InfrastructureCluster ADD CiId INT NULL
        CONSTRAINT FK_InfrastructureCluster_Ci FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId);
    PRINT 'added sad.InfrastructureCluster.CiId';
END
ELSE PRINT 'sad.InfrastructureCluster.CiId already present - skipped';
GO

IF COL_LENGTH('sad.ClusterNode', 'CiId') IS NULL
BEGIN
    ALTER TABLE sad.ClusterNode ADD CiId INT NULL
        CONSTRAINT FK_ClusterNode_Ci FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId);
    PRINT 'added sad.ClusterNode.CiId';
END
ELSE PRINT 'sad.ClusterNode.CiId already present - skipped';
GO

IF COL_LENGTH('sad.Neighborhood', 'CiId') IS NULL
BEGIN
    ALTER TABLE sad.Neighborhood ADD CiId INT NULL
        CONSTRAINT FK_Neighborhood_Ci FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId);
    PRINT 'added sad.Neighborhood.CiId';
END
ELSE PRINT 'sad.Neighborhood.CiId already present - skipped';
GO

-- =============================================================================
-- 6. Classes the estate did not have
-- =============================================================================
-- The data centre was an NVARCHAR(200) stored twice - on Neighborhood and again
-- on InfrastructureCluster - so it could hold no attributes at all. A site has a
-- tier, a region and a DR partner; there was nowhere to put any of them, and
-- nothing stopped the two copies disagreeing.
IF OBJECT_ID('sad.CiDataCentre', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiDataCentre
    (
        CiId          INT           NOT NULL,
        Code          VARCHAR(40)   NOT NULL,
        City          NVARCHAR(80)  NOT NULL,
        Region        NVARCHAR(80)  NOT NULL,
        Tier          VARCHAR(12)   NULL,      -- Tier III / Tier IV
        PairedWithCiId INT          NULL,      -- metro DR partner
        CONSTRAINT PK_CiDataCentre PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiDataCentre_Ci     FOREIGN KEY (CiId)           REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT FK_CiDataCentre_Paired FOREIGN KEY (PairedWithCiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiDataCentre_Code UNIQUE (Code)
    );
    PRINT 'created sad.CiDataCentre';
END
ELSE PRINT 'sad.CiDataCentre already present - skipped';
GO

-- The VM layer is the one that changes what the platform can say. Without it,
-- "four VMs landing on two physical hosts" is not expressible and resiliency
-- can only ever count nodes.
IF OBJECT_ID('sad.CiVmInstance', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiVmInstance
    (
        CiId        INT           NOT NULL,
        VmName      VARCHAR(120)  NOT NULL,
        VcpuCount   INT           NOT NULL,
        MemoryGb    INT           NOT NULL,
        DiskGb      INT           NOT NULL,
        PowerState  VARCHAR(20)   NOT NULL CONSTRAINT DF_CiVmInstance_PowerState DEFAULT ('On'),
        CONSTRAINT PK_CiVmInstance PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiVmInstance_Ci FOREIGN KEY (CiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiVmInstance_Name UNIQUE (VmName),
        CONSTRAINT CK_CiVmInstance_PowerState CHECK (PowerState IN ('On','Off','Suspended'))
    );
    PRINT 'created sad.CiVmInstance';
END
ELSE PRINT 'sad.CiVmInstance already present - skipped';
GO

IF OBJECT_ID('sad.CiDatabaseInstance', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiDatabaseInstance
    (
        CiId          INT           NOT NULL,
        InstanceName  VARCHAR(120)  NOT NULL,
        Engine        VARCHAR(40)   NOT NULL,   -- SQLServer | PostgreSQL | Oracle | MongoDB
        Version       VARCHAR(30)   NULL,
        SizeGb        INT           NULL,
        IsClustered   BIT           NOT NULL CONSTRAINT DF_CiDatabaseInstance_Clustered DEFAULT (0),
        CONSTRAINT PK_CiDatabaseInstance PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiDatabaseInstance_Ci FOREIGN KEY (CiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiDatabaseInstance_Name UNIQUE (InstanceName)
    );
    PRINT 'created sad.CiDatabaseInstance';
END
ELSE PRINT 'sad.CiDatabaseInstance already present - skipped';
GO

-- A business service is what the bank actually cares about losing. Incidents point
-- at a technical CI; the service is what turns "host-12 is down" into "Wealth
-- Ledger is degraded", which is the sentence that reaches a person.
IF OBJECT_ID('sad.CiBusinessService', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiBusinessService
    (
        CiId           INT           NOT NULL,
        ServiceCode    VARCHAR(40)   NOT NULL,
        ServiceName    NVARCHAR(160) NOT NULL,
        Criticality    VARCHAR(20)   NOT NULL,   -- Platinum | Gold | Silver | Bronze
        BusinessUnit   NVARCHAR(80)  NULL,
        RtoMinutes     INT           NULL,
        RpoMinutes     INT           NULL,
        CONSTRAINT PK_CiBusinessService PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiBusinessService_Ci FOREIGN KEY (CiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiBusinessService_Code UNIQUE (ServiceCode),
        CONSTRAINT CK_CiBusinessService_Criticality CHECK (Criticality IN ('Platinum','Gold','Silver','Bronze'))
    );
    PRINT 'created sad.CiBusinessService';
END
ELSE PRINT 'sad.CiBusinessService already present - skipped';
GO

IF OBJECT_ID('sad.CiLoadBalancer', 'U') IS NULL
BEGIN
    CREATE TABLE sad.CiLoadBalancer
    (
        CiId        INT           NOT NULL,
        DeviceName  VARCHAR(120)  NOT NULL,
        Vendor      VARCHAR(60)   NULL,
        VirtualIp   VARCHAR(45)   NULL,
        IsHaPair    BIT           NOT NULL CONSTRAINT DF_CiLoadBalancer_Ha DEFAULT (1),
        CONSTRAINT PK_CiLoadBalancer PRIMARY KEY CLUSTERED (CiId),
        CONSTRAINT FK_CiLoadBalancer_Ci FOREIGN KEY (CiId) REFERENCES sad.ConfigurationItem (CiId),
        CONSTRAINT UQ_CiLoadBalancer_Name UNIQUE (DeviceName)
    );
    PRINT 'created sad.CiLoadBalancer';
END
ELSE PRINT 'sad.CiLoadBalancer already present - skipped';
GO

-- =============================================================================
-- 7. Tasks point at a CI
-- =============================================================================
-- CmdbCiId is the incident's subject, the way ServiceNow's incident.cmdb_ci is.
-- ApplicationId, ClusterId and NodeId remain, DERIVED from the graph, because the
-- scoring path filters on them constantly and a recursive walk per candidate would
-- be pointless cost. They are an index, never the truth: if they disagree with
-- CiRelationship, the graph is right and they are stale.
IF COL_LENGTH('sad.Incident', 'CmdbCiId') IS NULL
BEGIN
    ALTER TABLE sad.Incident ADD
        CmdbCiId            INT NULL CONSTRAINT FK_Incident_Ci        FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId),
        BusinessServiceCiId INT NULL CONSTRAINT FK_Incident_Service   FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId);
    PRINT 'added sad.Incident.CmdbCiId / BusinessServiceCiId';
END
ELSE PRINT 'sad.Incident CI columns already present - skipped';
GO

IF COL_LENGTH('sad.Change', 'CmdbCiId') IS NULL
BEGIN
    ALTER TABLE sad.Change ADD
        CmdbCiId            INT NULL CONSTRAINT FK_Change_Ci      FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId),
        BusinessServiceCiId INT NULL CONSTRAINT FK_Change_Service FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId);
    PRINT 'added sad.Change.CmdbCiId / BusinessServiceCiId';
END
ELSE PRINT 'sad.Change CI columns already present - skipped';
GO

IF COL_LENGTH('sad.Problem', 'CmdbCiId') IS NULL
BEGIN
    ALTER TABLE sad.Problem ADD
        CmdbCiId            INT NULL CONSTRAINT FK_Problem_Ci      FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId),
        BusinessServiceCiId INT NULL CONSTRAINT FK_Problem_Service FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId);
    PRINT 'added sad.Problem.CmdbCiId / BusinessServiceCiId';
END
ELSE PRINT 'sad.Problem CI columns already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Incident_CmdbCi')
   AND COL_LENGTH('sad.Incident', 'CmdbCiId') IS NOT NULL
BEGIN
    CREATE INDEX IX_Incident_CmdbCi ON sad.Incident (CmdbCiId) INCLUDE (Severity, OpenedAt);
    PRINT 'created IX_Incident_CmdbCi';
END
GO

-- =============================================================================
-- 8. Backfill - every existing row becomes a CI
-- =============================================================================
-- Written NOT EXISTS so re-running adds nothing. SysId is derived from the class
-- and the natural key rather than randomly, so a re-run of this migration against
-- a rebuilt database produces the same identity for the same logical thing - the
-- whole point of having a SysId at all.

-- Data centres, promoted from the string they used to be.
INSERT INTO sad.ConfigurationItem (SysId, Name, ClassName, Environment, DiscoverySource, FirstDiscovered, LastDiscovered)
SELECT DISTINCT
       LOWER(CONVERT(CHAR(32), HASHBYTES('MD5', 'dc:' + n.DataCenter), 2)),
       n.DataCenter, 'cmdb_ci_datacenter', 'Production', 'Import', SYSUTCDATETIME(), SYSUTCDATETIME()
FROM   sad.Neighborhood n
WHERE  NOT EXISTS (SELECT 1 FROM sad.ConfigurationItem c
                   WHERE c.ClassName = 'cmdb_ci_datacenter' AND c.Name = n.DataCenter);
DECLARE @n1 INT = (SELECT COUNT(*) FROM sad.ConfigurationItem WHERE ClassName = 'cmdb_ci_datacenter');
PRINT CONCAT('data centre CIs: ', @n1);
GO

INSERT INTO sad.CiDataCentre (CiId, Code, City, Region)
SELECT c.CiId,
       REPLACE(REPLACE(c.Name, ' ', '-'), '--', '-'),
       LEFT(c.Name, CASE WHEN CHARINDEX('-', c.Name) > 0 THEN CHARINDEX('-', c.Name) - 1 ELSE LEN(c.Name) END),
       ISNULL((SELECT TOP 1 n.Region FROM sad.Neighborhood n WHERE n.DataCenter = c.Name), 'Unknown')
FROM   sad.ConfigurationItem c
WHERE  c.ClassName = 'cmdb_ci_datacenter'
  AND  NOT EXISTS (SELECT 1 FROM sad.CiDataCentre d WHERE d.CiId = c.CiId);
GO

-- Zones (the existing Neighborhood rows).
INSERT INTO sad.ConfigurationItem (SysId, Name, ClassName, Environment, DiscoverySource, FirstDiscovered, LastDiscovered)
SELECT LOWER(CONVERT(CHAR(32), HASHBYTES('MD5', 'zone:' + n.NeighborhoodCode), 2)),
       n.NeighborhoodCode, 'cmdb_ci_zone', 'Production', 'Import', SYSUTCDATETIME(), SYSUTCDATETIME()
FROM   sad.Neighborhood n
WHERE  n.CiId IS NULL;
UPDATE n SET n.CiId = c.CiId
FROM   sad.Neighborhood n
JOIN   sad.ConfigurationItem c ON c.ClassName = 'cmdb_ci_zone' AND c.Name = n.NeighborhoodCode
WHERE  n.CiId IS NULL;
DECLARE @n2 INT = (SELECT COUNT(*) FROM sad.Neighborhood WHERE CiId IS NOT NULL);
PRINT CONCAT('zone CIs linked: ', @n2);
GO

-- Clusters.
INSERT INTO sad.ConfigurationItem (SysId, Name, ClassName, Environment, DataClassification, DiscoverySource, FirstDiscovered, LastDiscovered)
SELECT LOWER(CONVERT(CHAR(32), HASHBYTES('MD5', 'cluster:' + i.ClusterCode), 2)),
       i.ClusterCode, 'cmdb_ci_cluster', i.Environment, i.ComplianceClassification, 'Discovery', SYSUTCDATETIME(), SYSUTCDATETIME()
FROM   sad.InfrastructureCluster i
WHERE  i.CiId IS NULL;
UPDATE i SET i.CiId = c.CiId
FROM   sad.InfrastructureCluster i
JOIN   sad.ConfigurationItem c ON c.ClassName = 'cmdb_ci_cluster' AND c.Name = i.ClusterCode
WHERE  i.CiId IS NULL;
DECLARE @n3 INT = (SELECT COUNT(*) FROM sad.InfrastructureCluster WHERE CiId IS NOT NULL);
PRINT CONCAT('cluster CIs linked: ', @n3);
GO

-- Physical hosts.
INSERT INTO sad.ConfigurationItem (SysId, Name, ClassName, Environment, DiscoverySource, FirstDiscovered, LastDiscovered)
SELECT LOWER(CONVERT(CHAR(32), HASHBYTES('MD5', 'server:' + n.HostName), 2)),
       n.HostName, 'cmdb_ci_server',
       (SELECT TOP 1 i.Environment FROM sad.InfrastructureCluster i WHERE i.ClusterId = n.ClusterId),
       'Discovery', SYSUTCDATETIME(), SYSUTCDATETIME()
FROM   sad.ClusterNode n
WHERE  n.CiId IS NULL;
UPDATE n SET n.CiId = c.CiId
FROM   sad.ClusterNode n
JOIN   sad.ConfigurationItem c ON c.ClassName = 'cmdb_ci_server' AND c.Name = n.HostName
WHERE  n.CiId IS NULL;
DECLARE @n4 INT = (SELECT COUNT(*) FROM sad.ClusterNode WHERE CiId IS NOT NULL);
PRINT CONCAT('server CIs linked: ', @n4);
GO

-- Applications.
INSERT INTO sad.ConfigurationItem (SysId, Name, ClassName, Environment, DataClassification, SupportGroupId, OwnedById, DiscoverySource, FirstDiscovered, LastDiscovered)
SELECT LOWER(CONVERT(CHAR(32), HASHBYTES('MD5', 'appl:' + a.ApplicationCode), 2)),
       a.ApplicationCode, 'cmdb_ci_appl', a.Environment, a.DataClassification,
       a.SupportGroupId, a.OwnerEmployeeId, 'Manual', SYSUTCDATETIME(), SYSUTCDATETIME()
FROM   sad.CmdbApplication a
WHERE  a.CiId IS NULL;
UPDATE a SET a.CiId = c.CiId
FROM   sad.CmdbApplication a
JOIN   sad.ConfigurationItem c ON c.ClassName = 'cmdb_ci_appl' AND c.Name = a.ApplicationCode
WHERE  a.CiId IS NULL;
DECLARE @n5 INT = (SELECT COUNT(*) FROM sad.CmdbApplication WHERE CiId IS NOT NULL);
PRINT CONCAT('application CIs linked: ', @n5);
GO

-- =============================================================================
-- 9. Backfill - containment edges
-- =============================================================================
-- Only what the existing columns can prove. The VM layer, dependency edges and
-- the deliberate defects come from the seed generator, which knows the topology
-- it invented; inventing them here would be guessing.
INSERT INTO sad.CiRelationship (ParentCiId, ChildCiId, TypeId)
SELECT dc.CiId, n.CiId, 5                     -- data centre Contains zone
FROM   sad.Neighborhood n
JOIN   sad.ConfigurationItem dc ON dc.ClassName = 'cmdb_ci_datacenter' AND dc.Name = n.DataCenter
WHERE  n.CiId IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ParentCiId = dc.CiId AND r.ChildCiId = n.CiId AND r.TypeId = 5);

INSERT INTO sad.CiRelationship (ParentCiId, ChildCiId, TypeId)
SELECT n.CiId, i.CiId, 5                      -- zone Contains cluster
FROM   sad.InfrastructureCluster i
JOIN   sad.Neighborhood n ON n.NeighborhoodId = i.NeighborhoodId
WHERE  i.CiId IS NOT NULL AND n.CiId IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ParentCiId = n.CiId AND r.ChildCiId = i.CiId AND r.TypeId = 5);

INSERT INTO sad.CiRelationship (ParentCiId, ChildCiId, TypeId)
SELECT i.CiId, nd.CiId, 3                     -- cluster Members server
FROM   sad.ClusterNode nd
JOIN   sad.InfrastructureCluster i ON i.ClusterId = nd.ClusterId
WHERE  nd.CiId IS NOT NULL AND i.CiId IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ParentCiId = i.CiId AND r.ChildCiId = nd.CiId AND r.TypeId = 3);

INSERT INTO sad.CiRelationship (ParentCiId, ChildCiId, TypeId)
SELECT i.CiId, a.CiId, 1                      -- cluster Runs application (until the seed inserts VMs between them)
FROM   sad.ApplicationHosting h
JOIN   sad.CmdbApplication a ON a.ApplicationId = h.ApplicationId
JOIN   sad.InfrastructureCluster i ON i.ClusterId = h.ClusterId
WHERE  a.CiId IS NOT NULL AND i.CiId IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ParentCiId = i.CiId AND r.ChildCiId = a.CiId AND r.TypeId = 1);

INSERT INTO sad.CiRelationship (ParentCiId, ChildCiId, TypeId)
SELECT t.CiId, s.CiId, 4                      -- target Used by source (source Depends on target)
FROM   sad.ApplicationDependency d
JOIN   sad.CmdbApplication s ON s.ApplicationId = d.SourceApplicationId
JOIN   sad.CmdbApplication t ON t.ApplicationId = d.TargetApplicationId
WHERE  s.CiId IS NOT NULL AND t.CiId IS NOT NULL AND s.CiId <> t.CiId
  AND  NOT EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ParentCiId = t.CiId AND r.ChildCiId = s.CiId AND r.TypeId = 4);

DECLARE @n6 INT = (SELECT COUNT(*) FROM sad.CiRelationship);
PRINT CONCAT('relationships: ', @n6);
GO

-- =============================================================================
-- 10. Backfill - tasks point at their CI
-- =============================================================================
-- Most specific wins: a node names a host, otherwise an application, otherwise a
-- cluster. That ordering matters - resolving to the cluster when the row names a
-- host would silently coarsen every incident in the corpus.
UPDATE i
SET    i.CmdbCiId = COALESCE(nd.CiId, ap.CiId, cl.CiId)
FROM   sad.Incident i
LEFT   JOIN sad.ClusterNode nd           ON nd.NodeId        = i.NodeId
LEFT   JOIN sad.CmdbApplication ap       ON ap.ApplicationId = i.ApplicationId
LEFT   JOIN sad.InfrastructureCluster cl ON cl.ClusterId     = i.ClusterId
WHERE  i.CmdbCiId IS NULL
  AND  COALESCE(nd.CiId, ap.CiId, cl.CiId) IS NOT NULL;
DECLARE @n7 INT = (SELECT COUNT(*) FROM sad.Incident WHERE CmdbCiId IS NOT NULL);
PRINT CONCAT('incidents linked to a CI: ', @n7);

UPDATE c
SET    c.CmdbCiId = COALESCE(nd.CiId, ap.CiId, cl.CiId)
FROM   sad.Change c
LEFT   JOIN sad.ClusterNode nd           ON nd.NodeId        = c.NodeId
LEFT   JOIN sad.CmdbApplication ap       ON ap.ApplicationId = c.ApplicationId
LEFT   JOIN sad.InfrastructureCluster cl ON cl.ClusterId     = c.ClusterId
WHERE  c.CmdbCiId IS NULL
  AND  COALESCE(nd.CiId, ap.CiId, cl.CiId) IS NOT NULL;

UPDATE p
SET    p.CmdbCiId = COALESCE(ap.CiId, cl.CiId)
FROM   sad.Problem p
LEFT   JOIN sad.CmdbApplication ap       ON ap.ApplicationId = p.ApplicationId
LEFT   JOIN sad.InfrastructureCluster cl ON cl.ClusterId     = p.ClusterId
WHERE  p.CmdbCiId IS NULL
  AND  COALESCE(ap.CiId, cl.CiId) IS NOT NULL;
GO

-- The primary affected CI, mirrored into task_ci so "everything this incident
-- touched" is one query rather than a union across three nullable columns.
INSERT INTO sad.TaskCi (TaskType, TaskId, CiId, IsPrimary)
SELECT 'Incident', i.IncidentId, i.CmdbCiId, 1
FROM   sad.Incident i
WHERE  i.CmdbCiId IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM sad.TaskCi t WHERE t.TaskType = 'Incident' AND t.TaskId = i.IncidentId AND t.CiId = i.CmdbCiId);

INSERT INTO sad.TaskCi (TaskType, TaskId, CiId, IsPrimary)
SELECT 'Change', c.ChangeId, c.CmdbCiId, 1
FROM   sad.Change c
WHERE  c.CmdbCiId IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM sad.TaskCi t WHERE t.TaskType = 'Change' AND t.TaskId = c.ChangeId AND t.CiId = c.CmdbCiId);

INSERT INTO sad.TaskCi (TaskType, TaskId, CiId, IsPrimary)
SELECT 'Problem', p.ProblemId, p.CmdbCiId, 1
FROM   sad.Problem p
WHERE  p.CmdbCiId IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM sad.TaskCi t WHERE t.TaskType = 'Problem' AND t.TaskId = p.ProblemId AND t.CiId = p.CmdbCiId);

DECLARE @n8 INT = (SELECT COUNT(*) FROM sad.TaskCi);
PRINT CONCAT('task-CI links: ', @n8);
GO

PRINT '--- migration 008 complete ---';
GO
