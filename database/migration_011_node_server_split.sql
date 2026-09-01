/*  Migration 011 - a node is not a server.

    WHAT WAS WRONG
    --------------
    sad.ClusterNode was both things at once: the record of a machine's membership
    in a cluster, and the machine. Migration 008 gave it a CiId of class
    cmdb_ci_server, which made the conflation explicit.

    It shows up as absurd hardware. Measured on the live estate:

        cores per NODE   min 2   avg 7    max 80
        memory per NODE  min 8   avg 27GB max 360GB

    Seven cores and 27 GB is not a server in a bank; it is a small VM. The row was
    carrying a cluster's capacity DIVIDED by its member count - per_node_cpu =
    total_cpu / node_count - which is the right number for "this member's share of
    the cluster" and the wrong number for "this physical machine". One row cannot
    be both, so it was silently the first while being named the second.

    THE HIERARCHY THIS RESTORES
    ---------------------------
        data centre -> zone -> cluster -> NODE -> SERVER -> VM -> app / db / storage

    ServiceNow keeps cmdb_ci_cluster_node distinct from cmdb_ci_server for the same
    reason. The node is a membership: which server participates in which cluster,
    in what role, holding what share. The server is hardware with a socket count.

    WHAT THIS BUYS BEYOND CORRECTNESS
    ---------------------------------
      - Server capacity becomes real without touching cluster capacity, so packing,
        utilisation targets and every forecast fixture are untouched.
      - A standalone database server simply has no node, instead of needing a
        fabricated cluster to exist in.
      - "Which physical machines are behind this application" stops being the same
        question as "which clusters", which is what resiliency has to distinguish.

    IDEMPOTENT by guard throughout.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

-- =============================================================================
-- 1. The node class
-- =============================================================================
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_ConfigurationItem_Class')
BEGIN
    ALTER TABLE sad.ConfigurationItem DROP CONSTRAINT CK_ConfigurationItem_Class;
END
GO

ALTER TABLE sad.ConfigurationItem WITH CHECK ADD CONSTRAINT CK_ConfigurationItem_Class
CHECK (ClassName IN (
    'cmdb_ci_appl', 'cmdb_ci_service', 'cmdb_ci_cluster', 'cmdb_ci_server',
    'cmdb_ci_vm_instance', 'cmdb_ci_db_instance', 'cmdb_ci_lb',
    'cmdb_ci_datacenter', 'cmdb_ci_zone',
    'cmdb_ci_netgear', 'cmdb_ci_storage_array', 'cmdb_ci_storage_volume',
    'cmdb_ci_msg_queue',
    -- added by 011: membership is its own class, as it is in ServiceNow
    'cmdb_ci_cluster_node'));
PRINT 'CK_ConfigurationItem_Class extended to 14 classes';
GO

-- =============================================================================
-- 2. Hardware moves onto the server
-- =============================================================================
-- sad.CiServer already exists for machines that belong to no cluster. It now
-- holds every physical machine, clustered or not, and carries the real hardware.
-- The columns below are what a person asks about a box, and none of them could be
-- answered before because the row was describing a share of a cluster instead.
IF COL_LENGTH('sad.CiServer', 'SocketCount') IS NULL
BEGIN
    ALTER TABLE sad.CiServer ADD
        SocketCount      INT           NULL,
        CoresPerSocket   INT           NULL,
        Manufacturer     VARCHAR(60)   NULL,
        Model            VARCHAR(80)   NULL,
        SerialNumber     VARCHAR(60)   NULL,
        RackPosition     VARCHAR(30)   NULL,
        ClusterId        INT           NULL,   -- NULL for standalone machines
        IsHypervisor     BIT           NOT NULL CONSTRAINT DF_CiServer_IsHypervisor DEFAULT (0);
    PRINT 'added hardware columns to sad.CiServer';
END
ELSE PRINT 'sad.CiServer hardware columns already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_CiServer_Cluster')
   AND COL_LENGTH('sad.CiServer', 'ClusterId') IS NOT NULL
BEGIN
    ALTER TABLE sad.CiServer WITH NOCHECK
        ADD CONSTRAINT FK_CiServer_Cluster FOREIGN KEY (ClusterId)
            REFERENCES sad.InfrastructureCluster (ClusterId);
    CREATE INDEX IX_CiServer_Cluster ON sad.CiServer (ClusterId) WHERE ClusterId IS NOT NULL;
    PRINT 'added FK_CiServer_Cluster';
END
GO

-- =============================================================================
-- 3. The node points at the server that fills it
-- =============================================================================
-- ClusterNode keeps its identity, its utilisation history and its share of the
-- cluster. What it gains is a pointer to the machine, so the two numbers stop
-- pretending to be one number.
IF COL_LENGTH('sad.ClusterNode', 'ServerCiId') IS NULL
BEGIN
    ALTER TABLE sad.ClusterNode ADD
        ServerCiId INT NULL CONSTRAINT FK_ClusterNode_Server
            FOREIGN KEY REFERENCES sad.ConfigurationItem (CiId),
        NodeRole   VARCHAR(20) NULL;   -- Primary | Secondary | Witness | Standby
    PRINT 'added sad.ClusterNode.ServerCiId / NodeRole';
END
ELSE PRINT 'sad.ClusterNode.ServerCiId already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_ClusterNode_NodeRole')
BEGIN
    ALTER TABLE sad.ClusterNode WITH NOCHECK ADD CONSTRAINT CK_ClusterNode_NodeRole
        CHECK (NodeRole IS NULL OR NodeRole IN ('Primary','Secondary','Witness','Standby'));
    PRINT 'added CK_ClusterNode_NodeRole';
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ClusterNode_Server')
   AND COL_LENGTH('sad.ClusterNode', 'ServerCiId') IS NOT NULL
BEGIN
    CREATE INDEX IX_ClusterNode_Server ON sad.ClusterNode (ServerCiId);
    PRINT 'created IX_ClusterNode_Server';
END
GO

-- =============================================================================
-- 4. Existing rows are reclassified, not rewritten
-- =============================================================================
-- Every ClusterNode CI was created as cmdb_ci_server by migration 008. It is a
-- node, so it is relabelled. The generator then creates the SERVER behind each
-- one; until it runs, ServerCiId is null and that is an honest "not yet known"
-- rather than a wrong answer.
UPDATE ci
SET    ci.ClassName = 'cmdb_ci_cluster_node'
FROM   sad.ConfigurationItem ci
JOIN   sad.ClusterNode n ON n.CiId = ci.CiId
WHERE  ci.ClassName = 'cmdb_ci_server';
GO

DECLARE @relabelled INT = (SELECT COUNT(*) FROM sad.ConfigurationItem WHERE ClassName = 'cmdb_ci_cluster_node');
PRINT CONCAT('cluster-node CIs: ', @relabelled);
GO

PRINT '--- migration 011 complete ---';
GO
