/*  Migration 013 - remove the shortcut edge that hides single points of failure.

    WHAT WAS WRONG
    --------------
    Migration 008 backfilled a direct edge

        cluster --Runs on::Runs--> application

    from sad.ApplicationHosting, with a comment saying it stood in "until the seed
    inserts VMs between them". The seed now does exactly that:

        cluster --Member of--> node --Runs on--> server --Hosted on--> VM
                --Runs on--> application

    Both edges exist, so an application has a VM parent AND a cluster parent. That
    is not merely redundant, it is wrong in the direction that matters.

    Resiliency counts DISTINCT parents per failure domain and takes the minimum.
    An application whose four VMs all sit on one hypervisor has one distinct
    physical parent - the finding. With the shortcut edge it also has a cluster
    parent, so a naive "distinct parents" count returns two and the collapse
    disappears. The estate would report redundancy it does not have, which is the
    precise failure this platform exists to prevent, produced by our own schema.

    seekanddestroy-ef measured 607 of 1,200 applications carrying a single point of
    failure that the old node-count score rated as fully redundant. Leaving this
    edge in place would have quietly restored a share of that blindness after we
    had gone to the trouble of removing it.

    WHY DELETE RATHER THAN RETYPE
    -----------------------------
    The hosting relationship is not lost: sad.ApplicationHosting still records
    which cluster an application is hosted on, and the graph still reaches the
    cluster from the application in four hops through real infrastructure. The
    shortcut only ever existed because the intermediate levels did not.

    Only edges with a genuine VM path are removed. An application the seed never
    placed on a VM keeps its cluster edge, because for that one the shortcut is
    the only thing connecting it to anything - and an unplaced application should
    report as unassessable rather than silently unlinked.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

DECLARE @before INT = (SELECT COUNT(*) FROM sad.CiRelationship);

;WITH shortcut AS (
    SELECT r.RelationshipId
    FROM   sad.CiRelationship r
    JOIN   sad.ConfigurationItem parent ON parent.CiId = r.ParentCiId
    JOIN   sad.ConfigurationItem child  ON child.CiId  = r.ChildCiId
    WHERE  r.TypeId = 1
      AND  parent.ClassName = 'cmdb_ci_cluster'
      AND  child.ClassName  = 'cmdb_ci_appl'
      -- only where the real path exists: some VM Runs-on edge reaches this app
      AND  EXISTS (
             SELECT 1
             FROM   sad.CiRelationship vr
             JOIN   sad.ConfigurationItem vm ON vm.CiId = vr.ParentCiId
             WHERE  vr.ChildCiId = r.ChildCiId
               AND  vr.TypeId = 1
               AND  vm.ClassName = 'cmdb_ci_vm_instance')
)
DELETE FROM sad.CiRelationship
WHERE  RelationshipId IN (SELECT RelationshipId FROM shortcut);

DECLARE @after INT = (SELECT COUNT(*) FROM sad.CiRelationship);
PRINT CONCAT('shortcut cluster->application edges removed: ', @before - @after);
PRINT CONCAT('relationships remaining: ', @after);
GO

-- Applications that still carry a cluster shortcut are the ones with no VM path.
-- Reported rather than fixed: it is a real statement about the estate, and a
-- resiliency engine should treat them as unassessable instead of guessing.
DECLARE @unplaced INT = (
    SELECT COUNT(DISTINCT r.ChildCiId)
    FROM   sad.CiRelationship r
    JOIN   sad.ConfigurationItem parent ON parent.CiId = r.ParentCiId
    JOIN   sad.ConfigurationItem child  ON child.CiId  = r.ChildCiId
    WHERE  r.TypeId = 1 AND parent.ClassName = 'cmdb_ci_cluster'
      AND  child.ClassName = 'cmdb_ci_appl');
PRINT CONCAT('applications with no VM path (cluster edge retained): ', @unplaced);
GO

PRINT '--- migration 013 complete ---';
GO
