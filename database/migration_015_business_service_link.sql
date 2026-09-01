/*  Migration 015 - the business service column nobody filled in.

    WHAT WAS WRONG
    --------------
    Migration 008 added BusinessServiceCiId to sad.Incident, sad.Change and
    sad.Problem, mirroring ServiceNow's incident.business_service. Nothing ever
    wrote to it. All 10,000 incidents carry NULL.

    seekanddestroy-c2 found it while building service-level aggregation and
    routed around it through the relationship graph, which works and should not
    have been necessary. A column that exists and is always NULL is worse than an
    absent one: it appears in the schema, it appears in a SELECT *, and the first
    person to trust it gets an empty result with no error to explain why.

    HOW THE LINK IS DERIVED
    -----------------------
    An incident names a CI. A business service reaches applications through
    Depends on::Used by, with the SERVICE as parent and the application as child -
    a service depends on the applications that deliver it. That direction is the
    opposite of what most people guess, and c2 confirmed it against the data
    rather than assuming it.

    So: incident -> its CI -> if that CI is an application, the service above it.

    WHEN AN APPLICATION SERVES SEVERAL SERVICES
    -------------------------------------------
    Some applications do, which is real. This column holds ONE service, so it
    takes the lowest CiId - deterministic, arbitrary, and documented as arbitrary.

    That is deliberate and it is why the column is a convenience rather than a
    source of truth. Anything that must be correct about multi-service impact has
    to traverse sad.TaskCi and the graph, where an incident legitimately counts
    once per service. c2 measured that: grouping 10,000 incidents by business
    service yields 10,993 rows, and the excess is the fact rather than a bug.

    A denormalised column that silently picks one of several answers is exactly
    the shape of the three nullable foreign keys migration 008 replaced. The
    difference is that this one is documented as a convenience, and the graph
    remains the authority.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

-- The service sitting above each application, lowest CiId where there are several.
IF OBJECT_ID('tempdb..#AppService') IS NOT NULL DROP TABLE #AppService;

SELECT app.CiId              AS ApplicationCiId,
       MIN(svc.CiId)         AS ServiceCiId,
       COUNT(DISTINCT svc.CiId) AS ServiceCount
INTO   #AppService
FROM   sad.CiRelationship r
JOIN   sad.ConfigurationItem svc ON svc.CiId = r.ParentCiId AND svc.ClassName = 'cmdb_ci_service'
JOIN   sad.ConfigurationItem app ON app.CiId = r.ChildCiId  AND app.ClassName = 'cmdb_ci_appl'
WHERE  r.TypeId = 4
GROUP BY app.CiId;

-- Hoisted into variables: T-SQL forbids a subquery inside PRINT/CONCAT, and the
-- error names neither the statement nor the reason.
DECLARE @multi   INT = (SELECT COUNT(*) FROM #AppService WHERE ServiceCount > 1);
DECLARE @mapped  INT = (SELECT COUNT(*) FROM #AppService);
PRINT CONCAT('applications mapped to a service: ', @mapped, ', of which multi-service: ', @multi);
GO

UPDATE i
SET    i.BusinessServiceCiId = s.ServiceCiId
FROM   sad.Incident i
JOIN   #AppService s ON s.ApplicationCiId = i.CmdbCiId
WHERE  i.BusinessServiceCiId IS NULL;

UPDATE c
SET    c.BusinessServiceCiId = s.ServiceCiId
FROM   sad.Change c
JOIN   #AppService s ON s.ApplicationCiId = c.CmdbCiId
WHERE  c.BusinessServiceCiId IS NULL;

UPDATE p
SET    p.BusinessServiceCiId = s.ServiceCiId
FROM   sad.Problem p
JOIN   #AppService s ON s.ApplicationCiId = p.CmdbCiId
WHERE  p.BusinessServiceCiId IS NULL;
GO

DECLARE @inc INT = (SELECT COUNT(*) FROM sad.Incident WHERE BusinessServiceCiId IS NOT NULL);
DECLARE @tot INT = (SELECT COUNT(*) FROM sad.Incident);
PRINT CONCAT('incidents with a business service: ', @inc, ' of ', @tot);

-- Incidents whose subject is a cluster or a host rather than an application have
-- no service above them, and that is correct rather than missing: a disk failing
-- on a hypervisor is not itself a business-service event until something running
-- on it is affected. Reported so the number is understood rather than chased.
PRINT CONCAT('incidents with no service (subject is not an application): ', @tot - @inc);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Incident_BusinessService')
BEGIN
    CREATE INDEX IX_Incident_BusinessService ON sad.Incident (BusinessServiceCiId)
        INCLUDE (Severity, OpenedAt, RootCauseCategory)
        WHERE BusinessServiceCiId IS NOT NULL;
    PRINT 'created IX_Incident_BusinessService';
END
GO

PRINT '--- migration 015 complete ---';
GO
