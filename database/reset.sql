/* =============================================================================
   SeekAndDestroy - reset script
   Drops every object in schema [sad] only. Never touches anything outside that
   schema, so it is safe to run against a shared PraveenDB database.

   Run with:
     sqlcmd -S LAPTOP-R6U8H616 -d PraveenDB -E -C -i database\reset.sql
============================================================================= */

SET NOCOUNT ON;
GO

IF EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'sad')
BEGIN
    -- Drop in strict reverse-dependency order.
    IF OBJECT_ID('sad.AgentAuditLog', 'U') IS NOT NULL DROP TABLE sad.AgentAuditLog;
    IF OBJECT_ID('sad.RecommendationDecision', 'U') IS NOT NULL DROP TABLE sad.RecommendationDecision;
    IF OBJECT_ID('sad.InfrastructureRecommendation', 'U') IS NOT NULL DROP TABLE sad.InfrastructureRecommendation;
    IF OBJECT_ID('sad.Investigation', 'U') IS NOT NULL DROP TABLE sad.Investigation;
    IF OBJECT_ID('sad.CapacityRequest', 'U') IS NOT NULL DROP TABLE sad.CapacityRequest;
    IF OBJECT_ID('sad.Incident', 'U') IS NOT NULL DROP TABLE sad.Incident;
    IF OBJECT_ID('sad.ApplicationDependency', 'U') IS NOT NULL DROP TABLE sad.ApplicationDependency;
    IF OBJECT_ID('sad.ApplicationUsage', 'U') IS NOT NULL DROP TABLE sad.ApplicationUsage;
    IF OBJECT_ID('sad.NodeUtilization', 'U') IS NOT NULL DROP TABLE sad.NodeUtilization;
    IF OBJECT_ID('sad.ClusterUtilization', 'U') IS NOT NULL DROP TABLE sad.ClusterUtilization;
    IF OBJECT_ID('sad.ApplicationHosting', 'U') IS NOT NULL DROP TABLE sad.ApplicationHosting;
    IF OBJECT_ID('sad.ClusterNode', 'U') IS NOT NULL DROP TABLE sad.ClusterNode;
    IF OBJECT_ID('sad.InfrastructureCluster', 'U') IS NOT NULL DROP TABLE sad.InfrastructureCluster;
    IF OBJECT_ID('sad.Neighborhood', 'U') IS NOT NULL DROP TABLE sad.Neighborhood;
    IF OBJECT_ID('sad.CmdbApplication', 'U') IS NOT NULL DROP TABLE sad.CmdbApplication;
    IF OBJECT_ID('sad.SupportGroup', 'U') IS NOT NULL DROP TABLE sad.SupportGroup;
    IF OBJECT_ID('sad.Employee', 'U') IS NOT NULL DROP TABLE sad.Employee;
END
GO

IF EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'sad')
BEGIN
    DECLARE @remaining INT = (
        SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = N'sad'
    );
    IF @remaining = 0
        EXEC('DROP SCHEMA sad');
END
GO
