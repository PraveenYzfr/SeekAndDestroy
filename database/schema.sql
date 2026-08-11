/* =============================================================================
   SeekAndDestroy - database schema
   Target: SQL Server (tested against SQL Server 2025) - database PraveenDB
   All objects live in schema [sad] so this file (and reset.sql) never touch
   anything else in the database.

   Run with:
     sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\schema.sql

   Design notes (see docs/business-rules.md for the full rationale):
   - CPU is stored in fractional cores (DECIMAL) throughout, memory/storage in GB.
   - Enumerated columns are constrained with CHECK (...) IN (...) mirroring
     ai-service/app/models/enums.py exactly - the two must be kept in sync.
   - Free-text OS columns (OperatingSystemRequirement, OperatingSystem) use the
     "Family/Version" convention (e.g. "Linux/RHEL9", "Windows/2022", "Any") and
     are intentionally not CHECK-constrained; app.models.enums.os_is_compatible
     only inspects the family prefix.
   - InfrastructureRecommendation.InvestigationId and AgentAuditLog.InvestigationId
     are additions beyond the field list in the original specification's table
     definitions: the specified endpoint GET /api/investigations/{id}/recommendations
     requires recommendations to be traceable to their owning investigation, and
     "audit every invocation" (security requirement) requires the same for audit
     log rows. Every other column matches the specification's field list exactly.
============================================================================= */

SET NOCOUNT ON;
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'sad')
BEGIN
    EXEC('CREATE SCHEMA sad AUTHORIZATION dbo');
END
GO

/* =============================================================================
   1. Employee
============================================================================= */
CREATE TABLE sad.Employee
(
    EmployeeId      INT IDENTITY(1,1) NOT NULL,
    EmployeeNumber  VARCHAR(20)        NOT NULL,
    DisplayName     NVARCHAR(200)      NOT NULL,
    Email           NVARCHAR(255)      NOT NULL,
    IsActive        BIT                NOT NULL CONSTRAINT DF_Employee_IsActive DEFAULT (1),
    -- Self-describing scrypt string, never a password and never reversible:
    --   scrypt$n=16384,r=8,p=1$<b64 salt>$<b64 derived key>
    -- Parameters are embedded per row so they can be raised later without
    -- invalidating existing hashes (see app/security/passwords.py). NULL means
    -- "no password set" - such an employee cannot sign in with a password, and
    -- there is no implicit default credential anywhere in this platform.
    PasswordHash      NVARCHAR(512)    NULL,
    PasswordUpdatedAt DATETIME2(3)     NULL,
    CONSTRAINT PK_Employee PRIMARY KEY CLUSTERED (EmployeeId),
    CONSTRAINT UQ_Employee_EmployeeNumber UNIQUE (EmployeeNumber),
    CONSTRAINT UQ_Employee_Email UNIQUE (Email)
);
GO

/* =============================================================================
   2. SupportGroup
============================================================================= */
CREATE TABLE sad.SupportGroup
(
    SupportGroupId  INT IDENTITY(1,1) NOT NULL,
    GroupName       NVARCHAR(200)      NOT NULL,
    Email           NVARCHAR(255)      NOT NULL,
    IsActive        BIT                NOT NULL CONSTRAINT DF_SupportGroup_IsActive DEFAULT (1),
    CONSTRAINT PK_SupportGroup PRIMARY KEY CLUSTERED (SupportGroupId),
    CONSTRAINT UQ_SupportGroup_GroupName UNIQUE (GroupName)
);
GO

/* =============================================================================
   3. CmdbApplication
============================================================================= */
CREATE TABLE sad.CmdbApplication
(
    ApplicationId                 INT IDENTITY(1,1) NOT NULL,
    ApplicationCode               VARCHAR(30)        NOT NULL,
    ApplicationName               NVARCHAR(200)      NOT NULL,
    Description                   NVARCHAR(1000)     NULL,
    BusinessCriticality           NVARCHAR(20)       NOT NULL,
    Environment                   NVARCHAR(20)       NOT NULL,
    LifecycleStatus               NVARCHAR(20)       NOT NULL,
    TechnologyPlatform            NVARCHAR(20)       NOT NULL,
    OperatingSystemRequirement    NVARCHAR(100)      NOT NULL,
    CpuRequirement                DECIMAL(10,2)      NOT NULL,
    MemoryRequirementGb           DECIMAL(12,2)      NOT NULL,
    StorageRequirementGb          DECIMAL(12,2)      NOT NULL,
    ExpectedAnnualGrowthPercent   DECIMAL(6,2)        NOT NULL CONSTRAINT DF_CmdbApplication_Growth DEFAULT (0),
    AvailabilityTier              NVARCHAR(10)       NOT NULL,
    DataClassification            NVARCHAR(20)       NOT NULL,
    PreferredLocation             NVARCHAR(100)      NULL,
    OwnerEmployeeId               INT                NOT NULL,
    SupportGroupId                INT                NOT NULL,
    CreatedAt                     DATETIME2(3)       NOT NULL CONSTRAINT DF_CmdbApplication_CreatedAt DEFAULT (SYSUTCDATETIME()),
    UpdatedAt                     DATETIME2(3)       NOT NULL CONSTRAINT DF_CmdbApplication_UpdatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_CmdbApplication PRIMARY KEY CLUSTERED (ApplicationId),
    CONSTRAINT UQ_CmdbApplication_ApplicationCode UNIQUE (ApplicationCode),
    CONSTRAINT FK_CmdbApplication_Owner FOREIGN KEY (OwnerEmployeeId) REFERENCES sad.Employee (EmployeeId),
    CONSTRAINT FK_CmdbApplication_SupportGroup FOREIGN KEY (SupportGroupId) REFERENCES sad.SupportGroup (SupportGroupId),
    CONSTRAINT CK_CmdbApplication_Criticality CHECK (BusinessCriticality IN ('Critical','High','Medium','Low')),
    CONSTRAINT CK_CmdbApplication_Environment CHECK (Environment IN ('Production','Staging','Test','Development')),
    CONSTRAINT CK_CmdbApplication_LifecycleStatus CHECK (LifecycleStatus IN ('Planned','Active','Deprecated','Decommissioning','Retired','Blocked','Unsupported')),
    CONSTRAINT CK_CmdbApplication_Platform CHECK (TechnologyPlatform IN ('Kubernetes','VMware','OpenShift','BareMetal','Hyper-V')),
    CONSTRAINT CK_CmdbApplication_AvailabilityTier CHECK (AvailabilityTier IN ('Tier-1','Tier-2','Tier-3')),
    CONSTRAINT CK_CmdbApplication_DataClassification CHECK (DataClassification IN ('Public','Internal','Confidential','Restricted')),
    CONSTRAINT CK_CmdbApplication_CpuRequirement CHECK (CpuRequirement > 0),
    CONSTRAINT CK_CmdbApplication_MemoryRequirement CHECK (MemoryRequirementGb > 0),
    CONSTRAINT CK_CmdbApplication_StorageRequirement CHECK (StorageRequirementGb > 0),
    CONSTRAINT CK_CmdbApplication_Growth CHECK (ExpectedAnnualGrowthPercent >= 0)
);
GO
CREATE INDEX IX_CmdbApplication_Owner ON sad.CmdbApplication (OwnerEmployeeId);
CREATE INDEX IX_CmdbApplication_SupportGroup ON sad.CmdbApplication (SupportGroupId);
CREATE INDEX IX_CmdbApplication_Environment ON sad.CmdbApplication (Environment);
GO

/* =============================================================================
   4. Neighborhood
   A mid-tier grouping within a data center (shared power/network/cooling
   domain - "pod") that infra engineers pick between DataCenter and Cluster
   when browsing by location. Added after the platform's initial build in
   response to infra-engineer feedback; the hierarchy stops here by design -
   Cluster -> ClusterNode already covers server/host granularity, and no
   separate VM entity is modeled (see docs/business-rules.md).
============================================================================= */
CREATE TABLE sad.Neighborhood
(
    NeighborhoodId    INT IDENTITY(1,1) NOT NULL,
    NeighborhoodCode  VARCHAR(30)        NOT NULL,
    NeighborhoodName  NVARCHAR(200)      NOT NULL,
    DataCenter        NVARCHAR(100)      NOT NULL,
    Region            NVARCHAR(100)      NOT NULL,
    LifecycleStatus   NVARCHAR(20)       NOT NULL CONSTRAINT DF_Neighborhood_LifecycleStatus DEFAULT ('Active'),
    CreatedAt         DATETIME2(3)       NOT NULL CONSTRAINT DF_Neighborhood_CreatedAt DEFAULT (SYSUTCDATETIME()),
    UpdatedAt         DATETIME2(3)       NOT NULL CONSTRAINT DF_Neighborhood_UpdatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_Neighborhood PRIMARY KEY CLUSTERED (NeighborhoodId),
    CONSTRAINT UQ_Neighborhood_NeighborhoodCode UNIQUE (NeighborhoodCode),
    CONSTRAINT CK_Neighborhood_LifecycleStatus CHECK (LifecycleStatus IN ('Planned','Active','Deprecated','Decommissioning','Retired','Blocked','Unsupported'))
);
GO
CREATE INDEX IX_Neighborhood_DataCenter ON sad.Neighborhood (DataCenter);
GO

/* =============================================================================
   5. InfrastructureCluster
============================================================================= */
CREATE TABLE sad.InfrastructureCluster
(
    ClusterId                  INT IDENTITY(1,1) NOT NULL,
    ClusterCode                VARCHAR(30)        NOT NULL,
    ClusterName                NVARCHAR(200)      NOT NULL,
    ClusterType                NVARCHAR(20)       NOT NULL,
    Platform                   NVARCHAR(20)       NOT NULL,
    OperatingSystem             NVARCHAR(100)      NOT NULL,
    Environment                NVARCHAR(20)       NOT NULL,
    NeighborhoodId             INT                NOT NULL,
    DataCenter                 NVARCHAR(100)      NOT NULL,
    Region                     NVARCHAR(100)      NOT NULL,
    LifecycleStatus            NVARCHAR(20)       NOT NULL,
    NodeCount                  INT                NOT NULL,
    TotalCpuCores               DECIMAL(10,2)      NOT NULL,
    TotalMemoryGb               DECIMAL(12,2)      NOT NULL,
    TotalStorageGb              DECIMAL(14,2)      NOT NULL,
    ReservedCpuPercent          DECIMAL(5,2)       NOT NULL CONSTRAINT DF_InfrastructureCluster_ReservedCpu DEFAULT (0),
    ReservedMemoryPercent       DECIMAL(5,2)       NOT NULL CONSTRAINT DF_InfrastructureCluster_ReservedMemory DEFAULT (0),
    MonthlyCost                DECIMAL(12,2)      NOT NULL,
    AvailabilityTier            NVARCHAR(10)       NOT NULL,
    ComplianceClassification    NVARCHAR(20)       NOT NULL,
    CreatedAt                  DATETIME2(3)       NOT NULL CONSTRAINT DF_InfrastructureCluster_CreatedAt DEFAULT (SYSUTCDATETIME()),
    UpdatedAt                  DATETIME2(3)       NOT NULL CONSTRAINT DF_InfrastructureCluster_UpdatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_InfrastructureCluster PRIMARY KEY CLUSTERED (ClusterId),
    CONSTRAINT UQ_InfrastructureCluster_ClusterCode UNIQUE (ClusterCode),
    CONSTRAINT FK_InfrastructureCluster_Neighborhood FOREIGN KEY (NeighborhoodId) REFERENCES sad.Neighborhood (NeighborhoodId),
    CONSTRAINT CK_InfrastructureCluster_ClusterType CHECK (ClusterType IN ('Kubernetes','VMware','OpenShift','BareMetal','Hyper-V')),
    CONSTRAINT CK_InfrastructureCluster_Platform CHECK (Platform IN ('Kubernetes','VMware','OpenShift','BareMetal','Hyper-V')),
    CONSTRAINT CK_InfrastructureCluster_Environment CHECK (Environment IN ('Production','Staging','Test','Development')),
    CONSTRAINT CK_InfrastructureCluster_LifecycleStatus CHECK (LifecycleStatus IN ('Planned','Active','Deprecated','Decommissioning','Retired','Blocked','Unsupported')),
    CONSTRAINT CK_InfrastructureCluster_AvailabilityTier CHECK (AvailabilityTier IN ('Tier-1','Tier-2','Tier-3')),
    CONSTRAINT CK_InfrastructureCluster_Compliance CHECK (ComplianceClassification IN ('Public','Internal','Confidential','Restricted')),
    CONSTRAINT CK_InfrastructureCluster_NodeCount CHECK (NodeCount >= 1),
    CONSTRAINT CK_InfrastructureCluster_TotalCpu CHECK (TotalCpuCores > 0),
    CONSTRAINT CK_InfrastructureCluster_TotalMemory CHECK (TotalMemoryGb > 0),
    CONSTRAINT CK_InfrastructureCluster_TotalStorage CHECK (TotalStorageGb > 0),
    CONSTRAINT CK_InfrastructureCluster_ReservedCpuPercent CHECK (ReservedCpuPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureCluster_ReservedMemoryPercent CHECK (ReservedMemoryPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureCluster_MonthlyCost CHECK (MonthlyCost >= 0)
);
GO
CREATE INDEX IX_InfrastructureCluster_Environment ON sad.InfrastructureCluster (Environment);
CREATE INDEX IX_InfrastructureCluster_LifecycleStatus ON sad.InfrastructureCluster (LifecycleStatus);
CREATE INDEX IX_InfrastructureCluster_Neighborhood ON sad.InfrastructureCluster (NeighborhoodId);
GO

/* =============================================================================
   6. ClusterNode
============================================================================= */
CREATE TABLE sad.ClusterNode
(
    NodeId          INT IDENTITY(1,1) NOT NULL,
    ClusterId       INT                NOT NULL,
    HostName        VARCHAR(255)       NOT NULL,
    IpAddress       VARCHAR(45)        NOT NULL,
    CpuCores        DECIMAL(10,2)      NOT NULL,
    MemoryGb        DECIMAL(12,2)      NOT NULL,
    StorageGb       DECIMAL(12,2)      NOT NULL,
    LifecycleStatus NVARCHAR(20)       NOT NULL,
    LastSeenAt      DATETIME2(3)       NOT NULL,
    MonthlyCost     DECIMAL(12,2)      NOT NULL,
    CreatedAt       DATETIME2(3)       NOT NULL CONSTRAINT DF_ClusterNode_CreatedAt DEFAULT (SYSUTCDATETIME()),
    UpdatedAt       DATETIME2(3)       NOT NULL CONSTRAINT DF_ClusterNode_UpdatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_ClusterNode PRIMARY KEY CLUSTERED (NodeId),
    CONSTRAINT UQ_ClusterNode_HostName UNIQUE (HostName),
    CONSTRAINT FK_ClusterNode_Cluster FOREIGN KEY (ClusterId) REFERENCES sad.InfrastructureCluster (ClusterId),
    CONSTRAINT CK_ClusterNode_LifecycleStatus CHECK (LifecycleStatus IN ('Planned','Active','Deprecated','Decommissioning','Retired','Blocked','Unsupported')),
    CONSTRAINT CK_ClusterNode_CpuCores CHECK (CpuCores > 0),
    CONSTRAINT CK_ClusterNode_MemoryGb CHECK (MemoryGb > 0),
    CONSTRAINT CK_ClusterNode_StorageGb CHECK (StorageGb > 0),
    CONSTRAINT CK_ClusterNode_MonthlyCost CHECK (MonthlyCost >= 0)
);
GO
CREATE INDEX IX_ClusterNode_Cluster ON sad.ClusterNode (ClusterId);
GO

/* =============================================================================
   7. ApplicationHosting
============================================================================= */
CREATE TABLE sad.ApplicationHosting
(
    HostingId            INT IDENTITY(1,1) NOT NULL,
    ApplicationId        INT                NOT NULL,
    ClusterId            INT                NOT NULL,
    NodeId               INT                NULL,
    Environment          NVARCHAR(20)       NOT NULL,
    AllocatedCpuCores    DECIMAL(10,2)      NOT NULL,
    AllocatedMemoryGb    DECIMAL(12,2)      NOT NULL,
    AllocatedStorageGb   DECIMAL(12,2)      NOT NULL,
    HostingStatus        NVARCHAR(20)       NOT NULL,
    IsPrimary            BIT                NOT NULL CONSTRAINT DF_ApplicationHosting_IsPrimary DEFAULT (1),
    HostedSince           DATETIME2(3)       NOT NULL,
    CreatedAt            DATETIME2(3)       NOT NULL CONSTRAINT DF_ApplicationHosting_CreatedAt DEFAULT (SYSUTCDATETIME()),
    UpdatedAt            DATETIME2(3)       NOT NULL CONSTRAINT DF_ApplicationHosting_UpdatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_ApplicationHosting PRIMARY KEY CLUSTERED (HostingId),
    CONSTRAINT FK_ApplicationHosting_Application FOREIGN KEY (ApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
    CONSTRAINT FK_ApplicationHosting_Cluster FOREIGN KEY (ClusterId) REFERENCES sad.InfrastructureCluster (ClusterId),
    CONSTRAINT FK_ApplicationHosting_Node FOREIGN KEY (NodeId) REFERENCES sad.ClusterNode (NodeId),
    CONSTRAINT CK_ApplicationHosting_Environment CHECK (Environment IN ('Production','Staging','Test','Development')),
    CONSTRAINT CK_ApplicationHosting_Status CHECK (HostingStatus IN ('Active','Planned','Migrating','Retired')),
    CONSTRAINT CK_ApplicationHosting_AllocatedCpu CHECK (AllocatedCpuCores >= 0),
    CONSTRAINT CK_ApplicationHosting_AllocatedMemory CHECK (AllocatedMemoryGb >= 0),
    CONSTRAINT CK_ApplicationHosting_AllocatedStorage CHECK (AllocatedStorageGb >= 0)
);
GO
CREATE INDEX IX_ApplicationHosting_Cluster ON sad.ApplicationHosting (ClusterId);
CREATE INDEX IX_ApplicationHosting_Application ON sad.ApplicationHosting (ApplicationId);
CREATE INDEX IX_ApplicationHosting_Node ON sad.ApplicationHosting (NodeId);
CREATE INDEX IX_ApplicationHosting_Status ON sad.ApplicationHosting (HostingStatus);
GO

/* =============================================================================
   8. ClusterUtilization
============================================================================= */
CREATE TABLE sad.ClusterUtilization
(
    UtilizationId       BIGINT IDENTITY(1,1) NOT NULL,
    ClusterId           INT                    NOT NULL,
    MetricDateTime       DATETIME2(3)           NOT NULL,
    CpuUsedPercent       DECIMAL(5,2)           NOT NULL,
    MemoryUsedPercent    DECIMAL(5,2)           NOT NULL,
    StorageUsedPercent   DECIMAL(5,2)           NOT NULL,
    NetworkUsedPercent   DECIMAL(5,2)           NOT NULL,
    ActiveWorkloadCount  INT                    NOT NULL,
    RequestVolume        BIGINT                 NOT NULL,
    CONSTRAINT PK_ClusterUtilization PRIMARY KEY CLUSTERED (UtilizationId),
    CONSTRAINT FK_ClusterUtilization_Cluster FOREIGN KEY (ClusterId) REFERENCES sad.InfrastructureCluster (ClusterId),
    CONSTRAINT UQ_ClusterUtilization_Cluster_Date UNIQUE (ClusterId, MetricDateTime),
    CONSTRAINT CK_ClusterUtilization_Cpu CHECK (CpuUsedPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_ClusterUtilization_Memory CHECK (MemoryUsedPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_ClusterUtilization_Storage CHECK (StorageUsedPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_ClusterUtilization_Network CHECK (NetworkUsedPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_ClusterUtilization_Workloads CHECK (ActiveWorkloadCount >= 0),
    CONSTRAINT CK_ClusterUtilization_RequestVolume CHECK (RequestVolume >= 0)
);
GO
CREATE INDEX IX_ClusterUtilization_Cluster_Date ON sad.ClusterUtilization (ClusterId, MetricDateTime DESC);
GO

/* =============================================================================
   9. NodeUtilization
============================================================================= */
CREATE TABLE sad.NodeUtilization
(
    UtilizationId       BIGINT IDENTITY(1,1) NOT NULL,
    NodeId              INT                    NOT NULL,
    MetricDateTime       DATETIME2(3)           NOT NULL,
    CpuUsedPercent       DECIMAL(5,2)           NOT NULL,
    MemoryUsedPercent    DECIMAL(5,2)           NOT NULL,
    StorageUsedPercent   DECIMAL(5,2)           NOT NULL,
    NetworkUsedPercent   DECIMAL(5,2)           NOT NULL,
    CONSTRAINT PK_NodeUtilization PRIMARY KEY CLUSTERED (UtilizationId),
    CONSTRAINT FK_NodeUtilization_Node FOREIGN KEY (NodeId) REFERENCES sad.ClusterNode (NodeId),
    CONSTRAINT UQ_NodeUtilization_Node_Date UNIQUE (NodeId, MetricDateTime),
    CONSTRAINT CK_NodeUtilization_Cpu CHECK (CpuUsedPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_NodeUtilization_Memory CHECK (MemoryUsedPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_NodeUtilization_Storage CHECK (StorageUsedPercent BETWEEN 0 AND 100),
    CONSTRAINT CK_NodeUtilization_Network CHECK (NetworkUsedPercent BETWEEN 0 AND 100)
);
GO
CREATE INDEX IX_NodeUtilization_Node_Date ON sad.NodeUtilization (NodeId, MetricDateTime DESC);
GO

/* =============================================================================
   10. ApplicationUsage
============================================================================= */
CREATE TABLE sad.ApplicationUsage
(
    UsageId              BIGINT IDENTITY(1,1) NOT NULL,
    ApplicationId        INT                    NOT NULL,
    UsageDateTime         DATETIME2(3)           NOT NULL,
    UserCount            INT                    NOT NULL,
    RequestCount          BIGINT                 NOT NULL,
    -- CpuConsumed is fractional cores, consistent with CmdbApplication.CpuRequirement.
    CpuConsumed           DECIMAL(10,2)          NOT NULL,
    MemoryConsumedGb      DECIMAL(12,2)          NOT NULL,
    StorageConsumedGb     DECIMAL(12,2)          NOT NULL,
    ResponseTimeMs        INT                    NOT NULL,
    CONSTRAINT PK_ApplicationUsage PRIMARY KEY CLUSTERED (UsageId),
    CONSTRAINT FK_ApplicationUsage_Application FOREIGN KEY (ApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
    CONSTRAINT UQ_ApplicationUsage_Application_Date UNIQUE (ApplicationId, UsageDateTime),
    CONSTRAINT CK_ApplicationUsage_UserCount CHECK (UserCount >= 0),
    CONSTRAINT CK_ApplicationUsage_RequestCount CHECK (RequestCount >= 0),
    CONSTRAINT CK_ApplicationUsage_CpuConsumed CHECK (CpuConsumed >= 0),
    CONSTRAINT CK_ApplicationUsage_MemoryConsumed CHECK (MemoryConsumedGb >= 0),
    CONSTRAINT CK_ApplicationUsage_StorageConsumed CHECK (StorageConsumedGb >= 0),
    CONSTRAINT CK_ApplicationUsage_ResponseTime CHECK (ResponseTimeMs >= 0)
);
GO
CREATE INDEX IX_ApplicationUsage_Application_Date ON sad.ApplicationUsage (ApplicationId, UsageDateTime DESC);
GO

/* =============================================================================
   11. ApplicationDependency
============================================================================= */
CREATE TABLE sad.ApplicationDependency
(
    DependencyId          INT IDENTITY(1,1) NOT NULL,
    SourceApplicationId   INT                NOT NULL,
    TargetApplicationId   INT                NULL,
    TargetClusterId       INT                NULL,
    DependencyType        NVARCHAR(30)       NOT NULL,
    LatencySensitivity    NVARCHAR(10)       NOT NULL,
    IsCritical            BIT                NOT NULL CONSTRAINT DF_ApplicationDependency_IsCritical DEFAULT (0),
    IsActive              BIT                NOT NULL CONSTRAINT DF_ApplicationDependency_IsActive DEFAULT (1),
    CONSTRAINT PK_ApplicationDependency PRIMARY KEY CLUSTERED (DependencyId),
    CONSTRAINT FK_ApplicationDependency_Source FOREIGN KEY (SourceApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
    CONSTRAINT FK_ApplicationDependency_TargetApplication FOREIGN KEY (TargetApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
    CONSTRAINT FK_ApplicationDependency_TargetCluster FOREIGN KEY (TargetClusterId) REFERENCES sad.InfrastructureCluster (ClusterId),
    CONSTRAINT UQ_ApplicationDependency_Edge UNIQUE (SourceApplicationId, TargetApplicationId, TargetClusterId, DependencyType),
    CONSTRAINT CK_ApplicationDependency_Type CHECK (DependencyType IN ('SynchronousApi','AsynchronousMessaging','Database','FileTransfer','Authentication')),
    CONSTRAINT CK_ApplicationDependency_Latency CHECK (LatencySensitivity IN ('High','Medium','Low')),
    CONSTRAINT CK_ApplicationDependency_OneTarget CHECK (
        (CASE WHEN TargetApplicationId IS NOT NULL THEN 1 ELSE 0 END)
      + (CASE WHEN TargetClusterId IS NOT NULL THEN 1 ELSE 0 END) = 1
    )
);
GO
CREATE INDEX IX_ApplicationDependency_Source ON sad.ApplicationDependency (SourceApplicationId);
CREATE INDEX IX_ApplicationDependency_TargetApplication ON sad.ApplicationDependency (TargetApplicationId);
CREATE INDEX IX_ApplicationDependency_TargetCluster ON sad.ApplicationDependency (TargetClusterId);
GO

/* =============================================================================
   12. Incident
============================================================================= */
CREATE TABLE sad.Incident
(
    IncidentId          INT IDENTITY(1,1) NOT NULL,
    ApplicationId       INT                NULL,
    ClusterId           INT                NULL,
    NodeId              INT                NULL,
    Severity            NVARCHAR(10)       NOT NULL,
    OpenedAt            DATETIME2(3)       NOT NULL,
    ClosedAt            DATETIME2(3)       NULL,
    Status               NVARCHAR(20)       NOT NULL,
    RootCauseCategory    NVARCHAR(30)       NOT NULL,
    CONSTRAINT PK_Incident PRIMARY KEY CLUSTERED (IncidentId),
    CONSTRAINT FK_Incident_Application FOREIGN KEY (ApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
    CONSTRAINT FK_Incident_Cluster FOREIGN KEY (ClusterId) REFERENCES sad.InfrastructureCluster (ClusterId),
    CONSTRAINT FK_Incident_Node FOREIGN KEY (NodeId) REFERENCES sad.ClusterNode (NodeId),
    CONSTRAINT CK_Incident_Severity CHECK (Severity IN ('Sev1','Sev2','Sev3','Sev4')),
    CONSTRAINT CK_Incident_Status CHECK (Status IN ('Open','InProgress','Resolved','Closed')),
    CONSTRAINT CK_Incident_RootCause CHECK (RootCauseCategory IN ('Capacity','Configuration','Hardware','Network','Software','Dependency','Unknown')),
    CONSTRAINT CK_Incident_ClosedAfterOpened CHECK (ClosedAt IS NULL OR ClosedAt >= OpenedAt),
    CONSTRAINT CK_Incident_HasSubject CHECK (
        ApplicationId IS NOT NULL OR ClusterId IS NOT NULL OR NodeId IS NOT NULL
    )
);
GO
CREATE INDEX IX_Incident_Cluster_OpenedAt ON sad.Incident (ClusterId, OpenedAt DESC);
CREATE INDEX IX_Incident_Application_OpenedAt ON sad.Incident (ApplicationId, OpenedAt DESC);
CREATE INDEX IX_Incident_Node_OpenedAt ON sad.Incident (NodeId, OpenedAt DESC);
GO

/* =============================================================================
   13. CapacityRequest
============================================================================= */
CREATE TABLE sad.CapacityRequest
(
    CapacityRequestId       INT IDENTITY(1,1) NOT NULL,
    ApplicationId           INT                NULL,
    RequestedBy             INT                NOT NULL,
    Environment              NVARCHAR(20)       NOT NULL,
    RequiredCpuCores         DECIMAL(10,2)      NOT NULL,
    RequiredMemoryGb         DECIMAL(12,2)      NOT NULL,
    RequiredStorageGb        DECIMAL(12,2)      NOT NULL,
    ExpectedGrowthPercent    DECIMAL(6,2)       NOT NULL CONSTRAINT DF_CapacityRequest_Growth DEFAULT (0),
    RequiredAvailabilityTier NVARCHAR(10)       NOT NULL,
    RequiredPlatform         NVARCHAR(20)       NOT NULL,
    PreferredLocation        NVARCHAR(100)      NULL,
    DataClassification       NVARCHAR(20)       NOT NULL,
    RequiredByDate           DATE               NULL,
    Status                    NVARCHAR(20)       NOT NULL CONSTRAINT DF_CapacityRequest_Status DEFAULT ('Open'),
    CreatedAt                DATETIME2(3)       NOT NULL CONSTRAINT DF_CapacityRequest_CreatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_CapacityRequest PRIMARY KEY CLUSTERED (CapacityRequestId),
    CONSTRAINT FK_CapacityRequest_Application FOREIGN KEY (ApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
    CONSTRAINT FK_CapacityRequest_RequestedBy FOREIGN KEY (RequestedBy) REFERENCES sad.Employee (EmployeeId),
    CONSTRAINT CK_CapacityRequest_Environment CHECK (Environment IN ('Production','Staging','Test','Development')),
    CONSTRAINT CK_CapacityRequest_AvailabilityTier CHECK (RequiredAvailabilityTier IN ('Tier-1','Tier-2','Tier-3')),
    CONSTRAINT CK_CapacityRequest_Platform CHECK (RequiredPlatform IN ('Kubernetes','VMware','OpenShift','BareMetal','Hyper-V')),
    CONSTRAINT CK_CapacityRequest_DataClassification CHECK (DataClassification IN ('Public','Internal','Confidential','Restricted')),
    CONSTRAINT CK_CapacityRequest_Status CHECK (Status IN ('Open','InAnalysis','Recommended','Approved','Rejected','Cancelled')),
    CONSTRAINT CK_CapacityRequest_Cpu CHECK (RequiredCpuCores > 0),
    CONSTRAINT CK_CapacityRequest_Memory CHECK (RequiredMemoryGb > 0),
    CONSTRAINT CK_CapacityRequest_Storage CHECK (RequiredStorageGb > 0),
    CONSTRAINT CK_CapacityRequest_Growth CHECK (ExpectedGrowthPercent >= 0)
);
GO
CREATE INDEX IX_CapacityRequest_Application ON sad.CapacityRequest (ApplicationId);
CREATE INDEX IX_CapacityRequest_Status ON sad.CapacityRequest (Status);
CREATE INDEX IX_CapacityRequest_RequestedBy ON sad.CapacityRequest (RequestedBy);
GO

/* =============================================================================
   14. Investigation
============================================================================= */
CREATE TABLE sad.Investigation
(
    InvestigationId    INT IDENTITY(1,1) NOT NULL,
    Query               NVARCHAR(2000)     NOT NULL,
    InvestigationType   NVARCHAR(20)       NOT NULL,
    Status               NVARCHAR(20)       NOT NULL CONSTRAINT DF_Investigation_Status DEFAULT ('Created'),
    CreatedBy           INT                NOT NULL,
    StartedAt           DATETIME2(3)       NOT NULL CONSTRAINT DF_Investigation_StartedAt DEFAULT (SYSUTCDATETIME()),
    CompletedAt         DATETIME2(3)       NULL,
    CONSTRAINT PK_Investigation PRIMARY KEY CLUSTERED (InvestigationId),
    CONSTRAINT FK_Investigation_CreatedBy FOREIGN KEY (CreatedBy) REFERENCES sad.Employee (EmployeeId),
    CONSTRAINT CK_Investigation_Type CHECK (InvestigationType IN ('Hosting','Capacity','RightSizing','Consolidation','Forecast','Question','Refused')),
    CONSTRAINT CK_Investigation_Status CHECK (Status IN ('Created','Running','AwaitingReview','Completed','Failed')),
    CONSTRAINT CK_Investigation_CompletedAfterStarted CHECK (CompletedAt IS NULL OR CompletedAt >= StartedAt)
);
GO
CREATE INDEX IX_Investigation_Status ON sad.Investigation (Status);
CREATE INDEX IX_Investigation_CreatedBy ON sad.Investigation (CreatedBy);
GO

/* =============================================================================
   15. InfrastructureRecommendation
============================================================================= */
CREATE TABLE sad.InfrastructureRecommendation
(
    RecommendationId            INT IDENTITY(1,1) NOT NULL,
    -- See file header: required by GET /api/investigations/{id}/recommendations.
    InvestigationId              INT                NOT NULL,
    CapacityRequestId            INT                NULL,
    ApplicationId                INT                NULL,
    RecommendationType           NVARCHAR(30)       NOT NULL,
    CandidateEntityType          NVARCHAR(20)       NOT NULL,
    CandidateEntityId            INT                NOT NULL,
    [Rank]                       INT                NOT NULL,
    EligibilityStatus            NVARCHAR(20)       NOT NULL,
    OverallScore                 DECIMAL(5,2)       NULL,
    CapacityScore                DECIMAL(5,2)       NULL,
    CompatibilityScore           DECIMAL(5,2)       NULL,
    CostScore                    DECIMAL(5,2)       NULL,
    ResiliencyScore              DECIMAL(5,2)       NULL,
    DependencyScore              DECIMAL(5,2)       NULL,
    RiskScore                    DECIMAL(5,2)       NULL,
    ProjectedCpuUtilization      DECIMAL(7,2)       NULL,
    ProjectedMemoryUtilization   DECIMAL(7,2)       NULL,
    ProjectedStorageUtilization  DECIMAL(7,2)       NULL,
    ProjectedHeadroomPercent     DECIMAL(7,2)       NULL,
    EstimatedMonthlyCost         DECIMAL(12,2)      NULL,
    Explanation                  NVARCHAR(MAX)      NULL,
    EvidenceJson                 NVARCHAR(MAX)      NULL,
    Status                        NVARCHAR(30)       NOT NULL CONSTRAINT DF_InfrastructureRecommendation_Status DEFAULT ('Proposed'),
    CreatedAt                    DATETIME2(3)       NOT NULL CONSTRAINT DF_InfrastructureRecommendation_CreatedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_InfrastructureRecommendation PRIMARY KEY CLUSTERED (RecommendationId),
    CONSTRAINT FK_InfrastructureRecommendation_Investigation FOREIGN KEY (InvestigationId) REFERENCES sad.Investigation (InvestigationId),
    CONSTRAINT FK_InfrastructureRecommendation_CapacityRequest FOREIGN KEY (CapacityRequestId) REFERENCES sad.CapacityRequest (CapacityRequestId),
    CONSTRAINT FK_InfrastructureRecommendation_Application FOREIGN KEY (ApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
    CONSTRAINT UQ_InfrastructureRecommendation_Candidate UNIQUE (InvestigationId, CandidateEntityType, CandidateEntityId),
    CONSTRAINT CK_InfrastructureRecommendation_Type CHECK (RecommendationType IN ('HostingPlacement','NewCapacity','ClusterRightSizing','ApplicationRightSizing','Consolidation','CapacityForecast')),
    CONSTRAINT CK_InfrastructureRecommendation_CandidateType CHECK (CandidateEntityType IN ('Cluster','Node','Application')),
    CONSTRAINT CK_InfrastructureRecommendation_Eligibility CHECK (EligibilityStatus IN ('Eligible','Rejected','Conditional')),
    CONSTRAINT CK_InfrastructureRecommendation_Status CHECK (Status IN ('Proposed','PendingReview','Approved','Rejected','MoreAnalysisRequested','Superseded')),
    CONSTRAINT CK_InfrastructureRecommendation_Rank CHECK ([Rank] >= 1),
    CONSTRAINT CK_InfrastructureRecommendation_OverallScore CHECK (OverallScore IS NULL OR OverallScore BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureRecommendation_CapacityScore CHECK (CapacityScore IS NULL OR CapacityScore BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureRecommendation_CompatibilityScore CHECK (CompatibilityScore IS NULL OR CompatibilityScore BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureRecommendation_CostScore CHECK (CostScore IS NULL OR CostScore BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureRecommendation_ResiliencyScore CHECK (ResiliencyScore IS NULL OR ResiliencyScore BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureRecommendation_DependencyScore CHECK (DependencyScore IS NULL OR DependencyScore BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureRecommendation_RiskScore CHECK (RiskScore IS NULL OR RiskScore BETWEEN 0 AND 100),
    CONSTRAINT CK_InfrastructureRecommendation_ProjectedCpu CHECK (ProjectedCpuUtilization IS NULL OR ProjectedCpuUtilization >= 0),
    CONSTRAINT CK_InfrastructureRecommendation_ProjectedMemory CHECK (ProjectedMemoryUtilization IS NULL OR ProjectedMemoryUtilization >= 0),
    CONSTRAINT CK_InfrastructureRecommendation_ProjectedStorage CHECK (ProjectedStorageUtilization IS NULL OR ProjectedStorageUtilization >= 0),
    CONSTRAINT CK_InfrastructureRecommendation_Cost CHECK (EstimatedMonthlyCost IS NULL OR EstimatedMonthlyCost >= 0)
);
GO
CREATE INDEX IX_Recommendation_Investigation ON sad.InfrastructureRecommendation (InvestigationId);
CREATE INDEX IX_Recommendation_CapacityRequest ON sad.InfrastructureRecommendation (CapacityRequestId);
CREATE INDEX IX_Recommendation_Application ON sad.InfrastructureRecommendation (ApplicationId);
CREATE INDEX IX_Recommendation_Status ON sad.InfrastructureRecommendation (Status);
GO

/* =============================================================================
   16. RecommendationDecision
============================================================================= */
CREATE TABLE sad.RecommendationDecision
(
    DecisionId        INT IDENTITY(1,1) NOT NULL,
    RecommendationId  INT                NOT NULL,
    Decision           NVARCHAR(30)       NOT NULL,
    DecisionReason     NVARCHAR(2000)     NULL,
    DecidedBy          INT                NOT NULL,
    DecidedAt          DATETIME2(3)       NOT NULL CONSTRAINT DF_RecommendationDecision_DecidedAt DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_RecommendationDecision PRIMARY KEY CLUSTERED (DecisionId),
    CONSTRAINT FK_RecommendationDecision_Recommendation FOREIGN KEY (RecommendationId) REFERENCES sad.InfrastructureRecommendation (RecommendationId),
    CONSTRAINT FK_RecommendationDecision_DecidedBy FOREIGN KEY (DecidedBy) REFERENCES sad.Employee (EmployeeId),
    CONSTRAINT CK_RecommendationDecision_Decision CHECK (Decision IN ('Approve','Reject','RequestMoreAnalysis'))
);
GO
CREATE INDEX IX_RecommendationDecision_Recommendation ON sad.RecommendationDecision (RecommendationId);
GO

/* =============================================================================
   17. AgentAuditLog
============================================================================= */
CREATE TABLE sad.AgentAuditLog
(
    AuditId          BIGINT IDENTITY(1,1) NOT NULL,
    -- Nullable: MCP tools may be invoked outside a formal Investigation (e.g. ad
    -- hoc via the interactive client); "audit every invocation" still applies.
    InvestigationId  INT                    NULL,
    GraphNode        NVARCHAR(100)          NULL,
    ToolName         NVARCHAR(100)          NOT NULL,
    InputJson        NVARCHAR(MAX)          NULL,
    OutputJson       NVARCHAR(MAX)          NULL,
    StartedAt        DATETIME2(3)           NOT NULL,
    CompletedAt      DATETIME2(3)           NULL,
    Success          BIT                    NULL,
    ErrorMessage     NVARCHAR(2000)         NULL,
    CONSTRAINT PK_AgentAuditLog PRIMARY KEY CLUSTERED (AuditId),
    CONSTRAINT FK_AgentAuditLog_Investigation FOREIGN KEY (InvestigationId) REFERENCES sad.Investigation (InvestigationId),
    CONSTRAINT CK_AgentAuditLog_CompletedAfterStarted CHECK (CompletedAt IS NULL OR CompletedAt >= StartedAt)
);
GO
CREATE INDEX IX_AgentAuditLog_Investigation ON sad.AgentAuditLog (InvestigationId);
CREATE INDEX IX_AgentAuditLog_ToolName ON sad.AgentAuditLog (ToolName);
CREATE INDEX IX_AgentAuditLog_StartedAt ON sad.AgentAuditLog (StartedAt DESC);
GO
