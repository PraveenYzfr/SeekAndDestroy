/*  Migration 004 - watermarks for differential indexing.

    index_all() clears the collection and re-embeds all ~2,400 documents. That
    is correct and it is what a schema change requires, but it is the wrong tool
    for "three nodes were decommissioned this morning": every unchanged document
    is re-sent to the embedding provider at 3072 dimensions to produce a vector
    identical to the one already stored.

    This table records, per source, how far the indexer has already read. A
    refresh then asks each source only for rows newer than its watermark.

    ONE ROW PER SOURCE, NOT ONE GLOBAL WATERMARK
    --------------------------------------------
    The sources do not advance together. Incidents arrive constantly, standards
    never change, and a failed refresh must be able to leave one source behind
    without rewinding the others - a single global timestamp would either
    re-index everything after any partial failure, or silently skip rows that
    arrived while a different source was being processed.

    LastSeenAt vs LastSeenId
    ------------------------
    Both, because the sources disagree on what they offer. CmdbApplication,
    InfrastructureCluster, ClusterNode and ApplicationHosting carry UpdatedAt.
    Incident has OpenedAt and ClosedAt but no UpdatedAt, so an edit that changes
    neither is invisible. ApplicationDependency has no timestamp at all and can
    only be followed by its IDENTITY column, which finds inserts and never
    finds an edit.

    That asymmetry is a property of the schema, not of this design, and it is
    why a periodic full rebuild remains necessary rather than optional.

    Idempotent, like migrations 001-003: re-running is a no-op.
*/

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.IndexWatermark'))
BEGIN
    CREATE TABLE sad.IndexWatermark
    (
        Source           NVARCHAR(40)  NOT NULL,
        -- Highest UpdatedAt/OpenedAt already indexed for this source. NULL means
        -- "never run", which is deliberately different from a zero date: it lets
        -- a first refresh be reported as such rather than as "nothing changed".
        LastSeenAt       DATETIME2(3)      NULL,
        -- Highest IDENTITY value already indexed, for sources with no timestamp.
        LastSeenId       INT               NULL,
        LastRunAt        DATETIME2(3)  NOT NULL,
        -- Documents written by the most recent run, not a running total. "It ran"
        -- and "it indexed something" must stay distinguishable.
        DocumentsIndexed INT           NOT NULL CONSTRAINT DF_IndexWatermark_Documents DEFAULT (0),
        CONSTRAINT PK_IndexWatermark PRIMARY KEY CLUSTERED (Source)
    );
    PRINT 'created sad.IndexWatermark';
END
ELSE
    PRINT 'sad.IndexWatermark already present - skipped';
GO

PRINT 'migration 004 complete';
GO
