/*  Migration 005 - index runs as tracked, resumable jobs.

    Indexing used to be a synchronous call inside an HTTP request: the caller
    held the connection for the whole run, nothing recorded that a run had
    happened, and a failure at 90% lost all of it because watermarks only
    advanced once, at the end. That is survivable for 2,400 documents and is not
    an indexing pipeline.

    This table is the job record. A trigger writes a Queued row and returns its
    RunId immediately; a worker claims it, reports progress against it, and
    finishes it. Nothing about the run lives only in a process.

    WHY SQL SERVER AND NOT REDIS
    ----------------------------
    The Redis queue carries the *request* - "somebody wants a refresh" - and is
    allowed to lose it, because a lost request is retried by a human pressing the
    button again. The run *record* is history: what ran, when, how far it got and
    why it stopped. Redis here is configured with a 256 MB cap and an eviction
    policy; history does not belong somewhere that evicts.

    HEARTBEATAT IS THE LIVENESS SIGNAL
    ----------------------------------
    Only one index job may execute at a time, and a worker killed mid-run cannot
    mark its own row - that is precisely the case where it has stopped executing.
    So the lock is held by a *fresh heartbeat*, not by the Running status: the
    worker touches HeartbeatAt on every batch, and a claim is refused only while
    some other run is Running AND its heartbeat is recent. A crashed worker
    therefore releases the lock by falling silent, rather than wedging indexing
    until somebody intervenes.

    Sweeping those rows to Abandoned is a separate, cosmetic step. It keeps the
    history honest - a run that stopped is not still Running - but it is not what
    frees the lock, and indexing works correctly even if it never happens.

    Idempotent, like migrations 001-004: re-running is a no-op.
*/

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.IndexRun'))
BEGIN
    CREATE TABLE sad.IndexRun
    (
        RunId            BIGINT IDENTITY(1,1) NOT NULL,
        -- 'refresh' indexes what changed; 'rebuild' clears and re-indexes all.
        Mode             NVARCHAR(20)   NOT NULL,
        -- Queued -> Running -> Succeeded | Failed | Abandoned
        Status           NVARCHAR(20)   NOT NULL,
        -- Employee number of whoever triggered it. Indexing spends money at the
        -- embedding provider, so an unattributable run is not acceptable.
        TriggeredBy      NVARCHAR(50)       NULL,
        QueuedAt         DATETIME2(3)   NOT NULL,
        StartedAt        DATETIME2(3)       NULL,
        CompletedAt      DATETIME2(3)       NULL,
        -- Touched on every batch. Staleness, not absence, means the worker died.
        HeartbeatAt      DATETIME2(3)       NULL,
        -- Progress, updated per batch rather than at the end, so a long run is
        -- observable while it is still running - which is the only time anyone
        -- actually wants to look at it.
        DocumentsIndexed INT            NOT NULL CONSTRAINT DF_IndexRun_Documents DEFAULT (0),
        BatchesCompleted INT            NOT NULL CONSTRAINT DF_IndexRun_Batches   DEFAULT (0),
        CurrentSource    NVARCHAR(40)       NULL,
        ErrorMessage     NVARCHAR(2000)     NULL,
        CONSTRAINT PK_IndexRun PRIMARY KEY CLUSTERED (RunId),
        CONSTRAINT CK_IndexRun_Mode   CHECK (Mode IN ('refresh','rebuild')),
        CONSTRAINT CK_IndexRun_Status CHECK (Status IN ('Queued','Running','Succeeded','Failed','Abandoned')),
        CONSTRAINT CK_IndexRun_CompletedAfterStarted CHECK (CompletedAt IS NULL OR StartedAt IS NULL OR CompletedAt >= StartedAt)
    );
    PRINT 'created sad.IndexRun';
END
ELSE
    PRINT 'sad.IndexRun already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_IndexRun_Status' AND object_id = OBJECT_ID('sad.IndexRun'))
BEGIN
    -- The worker asks "is anything already Running?" before claiming a job, on
    -- every claim. Without this that is a scan of the whole run history.
    CREATE INDEX IX_IndexRun_Status ON sad.IndexRun (Status, HeartbeatAt);
    PRINT 'created IX_IndexRun_Status';
END
ELSE
    PRINT 'IX_IndexRun_Status already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_IndexRun_QueuedAt' AND object_id = OBJECT_ID('sad.IndexRun'))
BEGIN
    CREATE INDEX IX_IndexRun_QueuedAt ON sad.IndexRun (QueuedAt DESC);
    PRINT 'created IX_IndexRun_QueuedAt';
END
ELSE
    PRINT 'IX_IndexRun_QueuedAt already present - skipped';
GO

PRINT 'migration 005 complete';
GO
