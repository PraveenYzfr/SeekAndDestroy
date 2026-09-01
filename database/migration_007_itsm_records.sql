/*  Migration 007 - changes, problems, and incidents that contain words.

    WHAT WAS WRONG
    --------------
    sad.Incident had nine columns and not one of them was free text:

        IncidentId, ApplicationId, ClusterId, NodeId,
        Severity, OpenedAt, ClosedAt, Status, RootCauseCategory

    No number. No description. No work notes. The three NVARCHAR columns are
    constrained enums - Sev1..Sev4, Open/InProgress/Resolved/Closed, and seven
    root-cause categories - so the same handful of values repeat across every
    row. An indexed incident document read:

        "Sev2 incident on cluster atl-03, opened 2026-04-11, status Resolved,
         root cause category Capacity."

    That is generated prose about a record, not the record. Hybrid retrieval was
    shipped on top of it: the BM25 tokeniser has a dedicated pattern for ITSM
    record numbers, and there were zero such tokens in the entire corpus. The
    sparse half had nothing to match because the estate had nothing to say.

    There was also no change management at all - no sad.Change, no CloseCode, no
    planned window - so "do not put this workload where three changes failed last
    month, and two more land on Thursday" could not be asked, let alone answered.

    WHY COMMENTS ARE THEIR OWN TABLE AND NOT A COLUMN
    -------------------------------------------------
    Because a ticket is not a chunk. The sections of a ticket answer different
    questions - what broke, what was tried, what fixed it - and embedding them as
    one blob averages the vector over all of them, so the one work note that
    answers the question is diluted by the ten that do not. One row per comment
    is what lets each become its own chunk, with its own position in the ticket
    and its own timestamp.

    Sequence is stored rather than derived from CreatedAt: two notes can share a
    timestamp to the millisecond, and "comment 7 of 11" needs a stable order to
    show in a retrieved chunk's context prefix.

    THE LINKS ARE THE POINT
    -----------------------
    Incident.CausedByChangeId and Problem.PermanentFixChangeId are what turn
    disconnected tickets into a chain: a change lands, incidents follow, a
    problem record explains them, a later change fixes it permanently. That
    chain is the thing worth retrieving. Without it this is a list.

    Both are nullable and neither is enforced beyond the foreign key - most
    incidents are not caused by a change, and saying so with NULL is more honest
    than inventing a cause.

    Idempotent, like migrations 001-006: re-running is a no-op.
*/

-- Filtered indexes (UQ_Incident_Number below) require these to be ON at CREATE
-- time, and refuse with a message about "SET options ... QUOTED_IDENTIFIER" that
-- names neither the index nor the filter. sqlcmd does not guarantee them, so
-- they are set explicitly here rather than depending on how this file is run -
-- db-init.sh and provision-databases.sh both invoke sqlcmd inside a container
-- with its own defaults.
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

-- =============================================================================
-- 1. sad.Change - change requests
-- =============================================================================
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.Change'))
BEGIN
    CREATE TABLE sad.Change
    (
        ChangeId            INT IDENTITY(1,1) NOT NULL,
        Number              NVARCHAR(20)   NOT NULL,
        ShortDescription    NVARCHAR(300)  NOT NULL,
        Description         NVARCHAR(MAX)      NULL,
        -- Normal goes to CAB; Standard is pre-approved; Emergency is raised
        -- after the fact. The mix matters: an estate whose changes are mostly
        -- Emergency is being operated reactively, and that is a real signal.
        Type                NVARCHAR(20)   NOT NULL,
        State               NVARCHAR(20)   NOT NULL,
        -- What it touches. All nullable: a change can target a cluster, a single
        -- node, an application, or infrastructure described only in prose.
        ClusterId           INT                NULL,
        NodeId              INT                NULL,
        ApplicationId       INT                NULL,
        -- The planned window is what makes a *future* change actionable. A
        -- workload should not be placed onto infrastructure that is about to be
        -- worked on, and that judgement needs the window, not just a flag.
        PlannedStart        DATETIME2(3)       NULL,
        PlannedEnd          DATETIME2(3)       NULL,
        ActualStart         DATETIME2(3)       NULL,
        ActualEnd           DATETIME2(3)       NULL,
        -- The outcome. Failed and BackedOut are the two that carry weight: a
        -- cluster where changes fail is unstable in a way utilisation does not
        -- show, and it is demonstrated rather than predicted.
        CloseCode           NVARCHAR(30)       NULL,
        CloseNotes          NVARCHAR(MAX)      NULL,
        ImplementationPlan  NVARCHAR(MAX)      NULL,
        BackoutPlan         NVARCHAR(MAX)      NULL,
        RiskAssessment      NVARCHAR(MAX)      NULL,
        AssignmentGroup     NVARCHAR(100)      NULL,
        -- A hard block, separate from any scheduled window: infrastructure can
        -- be frozen for reasons that are not themselves a change - an audit, a
        -- trading period, a migration in flight.
        FreezeUntil         DATETIME2(3)       NULL,
        CreatedAt           DATETIME2(3)   NOT NULL CONSTRAINT DF_Change_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt           DATETIME2(3)   NOT NULL CONSTRAINT DF_Change_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_Change PRIMARY KEY CLUSTERED (ChangeId),
        CONSTRAINT UQ_Change_Number UNIQUE (Number),
        CONSTRAINT FK_Change_Cluster     FOREIGN KEY (ClusterId)     REFERENCES sad.InfrastructureCluster (ClusterId),
        CONSTRAINT FK_Change_Node        FOREIGN KEY (NodeId)        REFERENCES sad.ClusterNode (NodeId),
        CONSTRAINT FK_Change_Application FOREIGN KEY (ApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
        CONSTRAINT CK_Change_Type  CHECK (Type IN ('Normal','Standard','Emergency')),
        CONSTRAINT CK_Change_State CHECK (State IN ('New','Assess','Authorize','Scheduled','Implement','Review','Closed','Cancelled')),
        CONSTRAINT CK_Change_CloseCode CHECK (CloseCode IS NULL OR CloseCode IN ('Successful','SuccessfulWithIssues','Failed','BackedOut')),
        CONSTRAINT CK_Change_PlannedWindow CHECK (PlannedEnd IS NULL OR PlannedStart IS NULL OR PlannedEnd >= PlannedStart),
        CONSTRAINT CK_Change_ActualWindow  CHECK (ActualEnd  IS NULL OR ActualStart  IS NULL OR ActualEnd  >= ActualStart)
    );
    PRINT 'created sad.Change';
END
ELSE
    PRINT 'sad.Change already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Change_Cluster_Planned' AND object_id = OBJECT_ID('sad.Change'))
BEGIN
    -- "What is scheduled on this cluster in the next N days" runs once per
    -- candidate during placement, so it must not scan the change history.
    CREATE INDEX IX_Change_Cluster_Planned ON sad.Change (ClusterId, PlannedStart) INCLUDE (State, PlannedEnd);
    PRINT 'created IX_Change_Cluster_Planned';
END
ELSE PRINT 'IX_Change_Cluster_Planned already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Change_Cluster_Outcome' AND object_id = OBJECT_ID('sad.Change'))
BEGIN
    -- "How many changes failed here in the last 90 days" - the other half of
    -- change risk, and a different access path from the planned-window query.
    CREATE INDEX IX_Change_Cluster_Outcome ON sad.Change (ClusterId, ActualEnd) INCLUDE (CloseCode);
    PRINT 'created IX_Change_Cluster_Outcome';
END
ELSE PRINT 'IX_Change_Cluster_Outcome already present - skipped';
GO

-- =============================================================================
-- 2. sad.Problem - root cause records and known errors
-- =============================================================================
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.Problem'))
BEGIN
    CREATE TABLE sad.Problem
    (
        ProblemId             INT IDENTITY(1,1) NOT NULL,
        Number                NVARCHAR(20)   NOT NULL,
        ShortDescription      NVARCHAR(300)  NOT NULL,
        Description           NVARCHAR(MAX)      NULL,
        -- These three are the highest-value text in the whole schema. A problem
        -- record is written to answer "why did this keep happening", which is
        -- the question a capacity planner is actually asking.
        RootCause             NVARCHAR(MAX)      NULL,
        Workaround            NVARCHAR(MAX)      NULL,
        FixNotes              NVARCHAR(MAX)      NULL,
        -- A known error with no permanent fix is the worst kind of history to
        -- inherit: it will recur, and everyone already knows why.
        IsKnownError          BIT            NOT NULL CONSTRAINT DF_Problem_IsKnownError DEFAULT (0),
        State                 NVARCHAR(20)   NOT NULL,
        PermanentFixChangeId  INT                NULL,
        ClusterId             INT                NULL,
        ApplicationId         INT                NULL,
        OpenedAt              DATETIME2(3)   NOT NULL,
        ClosedAt              DATETIME2(3)       NULL,
        CreatedAt             DATETIME2(3)   NOT NULL CONSTRAINT DF_Problem_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt             DATETIME2(3)   NOT NULL CONSTRAINT DF_Problem_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_Problem PRIMARY KEY CLUSTERED (ProblemId),
        CONSTRAINT UQ_Problem_Number UNIQUE (Number),
        CONSTRAINT FK_Problem_Change      FOREIGN KEY (PermanentFixChangeId) REFERENCES sad.Change (ChangeId),
        CONSTRAINT FK_Problem_Cluster     FOREIGN KEY (ClusterId)     REFERENCES sad.InfrastructureCluster (ClusterId),
        CONSTRAINT FK_Problem_Application FOREIGN KEY (ApplicationId) REFERENCES sad.CmdbApplication (ApplicationId),
        CONSTRAINT CK_Problem_State CHECK (State IN ('New','Assess','RootCauseAnalysis','FixInProgress','Resolved','Closed')),
        CONSTRAINT CK_Problem_ClosedAfterOpened CHECK (ClosedAt IS NULL OR ClosedAt >= OpenedAt)
    );
    PRINT 'created sad.Problem';
END
ELSE
    PRINT 'sad.Problem already present - skipped';
GO

-- =============================================================================
-- 3. sad.Incident - the columns that make it a ticket rather than a row
-- =============================================================================
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('sad.Incident') AND name = 'Number')
BEGIN
    -- Nullable, because 61 incidents already exist without one. The seed
    -- regenerates them; a NULL here means "predates this migration" rather
    -- than blocking the migration on data that is about to be replaced.
    ALTER TABLE sad.Incident ADD
        Number            NVARCHAR(20)      NULL,
        ShortDescription  NVARCHAR(300)     NULL,
        Description       NVARCHAR(MAX)     NULL,
        CloseNotes        NVARCHAR(MAX)     NULL,
        AssignmentGroup   NVARCHAR(100)     NULL,
        Impact            NVARCHAR(20)      NULL,
        Urgency           NVARCHAR(20)      NULL,
        ProblemId         INT               NULL,
        CausedByChangeId  INT               NULL;
    PRINT 'added ITSM columns to sad.Incident';
END
ELSE
    PRINT 'sad.Incident ITSM columns already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Incident_Problem')
BEGIN
    ALTER TABLE sad.Incident ADD CONSTRAINT FK_Incident_Problem
        FOREIGN KEY (ProblemId) REFERENCES sad.Problem (ProblemId);
    PRINT 'created FK_Incident_Problem';
END
ELSE PRINT 'FK_Incident_Problem already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Incident_CausedByChange')
BEGIN
    ALTER TABLE sad.Incident ADD CONSTRAINT FK_Incident_CausedByChange
        FOREIGN KEY (CausedByChangeId) REFERENCES sad.Change (ChangeId);
    PRINT 'created FK_Incident_CausedByChange';
END
ELSE PRINT 'FK_Incident_CausedByChange already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_Incident_Number' AND object_id = OBJECT_ID('sad.Incident'))
BEGIN
    -- Filtered, so the pre-migration rows with a NULL number do not collide
    -- with each other while the constraint still holds for every real ticket.
    CREATE UNIQUE INDEX UQ_Incident_Number ON sad.Incident (Number) WHERE Number IS NOT NULL;
    PRINT 'created UQ_Incident_Number';
END
ELSE PRINT 'UQ_Incident_Number already present - skipped';
GO

-- =============================================================================
-- 4. Comments - one row per note, because one chunk per note
-- =============================================================================
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.IncidentComment'))
BEGIN
    CREATE TABLE sad.IncidentComment
    (
        CommentId   BIGINT IDENTITY(1,1) NOT NULL,
        IncidentId  INT            NOT NULL,
        -- Explicit, not derived from CreatedAt: notes can share a timestamp,
        -- and "comment 7 of 11" needs a stable order for the context prefix a
        -- retrieved chunk carries.
        Sequence    INT            NOT NULL,
        CreatedAt   DATETIME2(3)   NOT NULL,
        CreatedBy   NVARCHAR(100)      NULL,
        -- work_note is internal engineering detail; additional_comment is
        -- customer-visible. They read differently and are worth distinguishing
        -- at retrieval time.
        Type        NVARCHAR(30)   NOT NULL,
        Text        NVARCHAR(MAX)  NOT NULL,
        CONSTRAINT PK_IncidentComment PRIMARY KEY CLUSTERED (CommentId),
        CONSTRAINT UQ_IncidentComment_Sequence UNIQUE (IncidentId, Sequence),
        CONSTRAINT FK_IncidentComment_Incident FOREIGN KEY (IncidentId) REFERENCES sad.Incident (IncidentId),
        CONSTRAINT CK_IncidentComment_Type CHECK (Type IN ('work_note','additional_comment'))
    );
    PRINT 'created sad.IncidentComment';
END
ELSE
    PRINT 'sad.IncidentComment already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('sad.ChangeComment'))
BEGIN
    CREATE TABLE sad.ChangeComment
    (
        CommentId   BIGINT IDENTITY(1,1) NOT NULL,
        ChangeId    INT            NOT NULL,
        Sequence    INT            NOT NULL,
        CreatedAt   DATETIME2(3)   NOT NULL,
        CreatedBy   NVARCHAR(100)      NULL,
        Type        NVARCHAR(30)   NOT NULL,
        Text        NVARCHAR(MAX)  NOT NULL,
        CONSTRAINT PK_ChangeComment PRIMARY KEY CLUSTERED (CommentId),
        CONSTRAINT UQ_ChangeComment_Sequence UNIQUE (ChangeId, Sequence),
        CONSTRAINT FK_ChangeComment_Change FOREIGN KEY (ChangeId) REFERENCES sad.Change (ChangeId),
        CONSTRAINT CK_ChangeComment_Type CHECK (Type IN ('work_note','additional_comment'))
    );
    PRINT 'created sad.ChangeComment';
END
ELSE
    PRINT 'sad.ChangeComment already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Incident_Cluster_Opened' AND object_id = OBJECT_ID('sad.Incident'))
BEGIN
    -- Incident history per cluster is read for the historical-performance
    -- sub-score on every candidate in a placement run. At 10,000 incidents
    -- that is the difference between an index seek and 256 table scans.
    CREATE INDEX IX_Incident_Cluster_Opened ON sad.Incident (ClusterId, OpenedAt) INCLUDE (Severity, Status);
    PRINT 'created IX_Incident_Cluster_Opened';
END
ELSE PRINT 'IX_Incident_Cluster_Opened already present - skipped';
GO

PRINT 'migration 007 complete';
GO
