/*  Migration 017 - the warning belongs on the table, not in a chat message.

    WHAT HAPPENED
    -------------
    TypeId 4, "Depends on::Used by", carries two different meanings:

        business service --Depends on--> application    (a service is delivered by
                                                         the applications beneath it)
        application      --Depends on--> application    (one app calls another)

    Nothing in the type distinguishes them; only the CLASS of the parent does.

    seekanddestroy-c2 hit this building service-level aggregation. Their join
    filtered "parent is a service" in the second step of a LEFT JOIN chain rather
    than in the first, so an application holding BOTH a service link and an
    unrelated app-to-app dependency produced spurious rows with a NULL service
    beside its correct match - inflating a "no business service" bucket with 259
    incidents that all had one. APP-PAYMENTS was the concrete case.

    The sharper part: their own independent verification query had copied the same
    join shape, so the test passed while the bug was live. They rewrote it as a CTE
    that pre-filters valid service edges - a genuinely different construction
    rather than the same one twice.

    WHY THE OVERLOAD STAYS
    ----------------------
    ServiceNow does the same thing. A service depending on the applications that
    deliver it and an application depending on another application are both
    "Depends on::Used by", and splitting them here would make the schema less
    recognisable to the people it is built for - which is the whole point of
    migration 008.

    So the type stays and the trap gets documented where somebody writing a query
    will actually encounter it: on the row itself, visible in any SELECT against
    cmdb_rel_type. A warning in a commit message helps nobody at the moment they
    are writing the join.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF COL_LENGTH('sad.CiRelationshipType', 'Guidance') IS NULL
BEGIN
    -- 1200, not 600. The type-4 note is the longest and the most important, and
    -- at 600 it was the one that truncated - silently leaving the trap it
    -- describes undocumented while every shorter note landed fine.
    ALTER TABLE sad.CiRelationshipType ADD Guidance NVARCHAR(1200) NULL;
    PRINT 'added sad.CiRelationshipType.Guidance';
END
ELSE
BEGIN
    -- Widen in place if an earlier run created it at 600.
    ALTER TABLE sad.CiRelationshipType ALTER COLUMN Guidance NVARCHAR(1200) NULL;
    PRINT 'sad.CiRelationshipType.Guidance widened to 1200';
END
GO

UPDATE sad.CiRelationshipType SET Guidance =
    'Parent is the physical host or VM, child is what it runs. Resiliency walks '
  + 'child->parent to count distinct hosts; blast radius walks parent->child.'
WHERE TypeId = 1;

UPDATE sad.CiRelationshipType SET Guidance =
    'Parent is the physical server, child is the VM. Containment: acyclic by '
  + 'construction. A VM has exactly one host at a time.'
WHERE TypeId = 2;

UPDATE sad.CiRelationshipType SET Guidance =
    'Parent is the cluster, child is the node. Containment. Note that a node is a '
  + 'membership record and the SERVER behind it is a separate CI - count servers, '
  + 'not nodes, when asking how many physical machines are involved.'
WHERE TypeId = 3;

UPDATE sad.CiRelationshipType SET Guidance =
    'OVERLOADED - filter on the PARENT CLASS. This type carries both '
  + 'service->application (a business service is delivered by its applications) '
  + 'and application->application (one app calls another). Nothing in the type '
  + 'distinguishes them. Put the class check in the FIRST join''s ON clause, not a '
  + 'later one: an application holding both kinds of edge will otherwise produce '
  + 'spurious NULL-service rows alongside its correct match, and the result looks '
  + 'like missing data rather than a join error. Also the only type that may CYCLE '
  + '- two applications calling each other is a real topology, so any traversal '
  + 'needs a visited-path guard and an explicit MAXRECURSION.'
WHERE TypeId = 4;

UPDATE sad.CiRelationshipType SET Guidance =
    'Parent contains the child: data centre contains zone, zone contains cluster. '
  + 'Containment, acyclic.'
WHERE TypeId = 5;

UPDATE sad.CiRelationshipType SET Guidance =
    'Parent provides, child consumes: array->volume, volume->VM, switch->server. '
  + 'NOT containment - a volume legitimately appears under several parents, which '
  + 'is exactly what makes it a shared failure domain. De-duplicate by CiId; do '
  + 'not assume a tree.'
WHERE TypeId = 6;
GO

DECLARE @undocumented INT = (SELECT COUNT(*) FROM sad.CiRelationshipType WHERE Guidance IS NULL);
PRINT CONCAT('relationship types without guidance: ', @undocumented);
GO

PRINT '--- migration 017 complete ---';
GO
