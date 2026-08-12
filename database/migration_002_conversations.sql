/* =============================================================================
   Migration 002 - Conversations, so a chat follow-up has a referent.

   Before this, every chat message was an independent investigation: "give me
   the options again" carried no reference to anything, classified as a general
   question, retrieved nothing and answered that it had no grounded
   information. A conversation gives those words something to point at.

   Idempotent: safe to run repeatedly, and safe against a database created from
   a schema.sql that already contains these objects (fresh installs get them
   from schema.sql section 18; existing databases get them from here).

   Design notes:

   - ConversationId is an opaque server-generated uuid4 hex, never chosen by
     the caller. The API checks that the signed-in employee owns the
     conversation before reading a single turn out of it; an id a caller could
     choose is an id they could guess.

   - Turn order is TurnId (IDENTITY) order. No TurnIndex column: a
     caller-computed "next index" is a lost update waiting to happen.

   - sad.Investigation.ConversationId is nullable and usually NULL - every
     investigation started from a structured screen or the MCP client has no
     conversation, and that is not a defect.

   Run:  sqlcmd -S <server> -d PraveenDB -E -C -i database\migration_002_conversations.sql
============================================================================= */

SET NOCOUNT ON;
GO

IF OBJECT_ID('sad.Conversation', 'U') IS NULL
BEGIN
    CREATE TABLE sad.Conversation
    (
        ConversationId  NVARCHAR(64)   NOT NULL,
        CreatedBy       INT            NOT NULL,
        StartedAt       DATETIME2(3)   NOT NULL CONSTRAINT DF_Conversation_StartedAt DEFAULT (SYSUTCDATETIME()),
        LastActivityAt  DATETIME2(3)   NOT NULL CONSTRAINT DF_Conversation_LastActivityAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_Conversation PRIMARY KEY CLUSTERED (ConversationId),
        CONSTRAINT FK_Conversation_CreatedBy FOREIGN KEY (CreatedBy) REFERENCES sad.Employee (EmployeeId),
        CONSTRAINT CK_Conversation_LastActivityAfterStarted CHECK (LastActivityAt >= StartedAt)
    );
    CREATE INDEX IX_Conversation_CreatedBy ON sad.Conversation (CreatedBy, LastActivityAt DESC);
    PRINT 'created sad.Conversation';
END
ELSE
    PRINT 'sad.Conversation already present - skipped';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sad.Investigation') AND name = 'ConversationId'
)
BEGIN
    ALTER TABLE sad.Investigation ADD ConversationId NVARCHAR(64) NULL;
    PRINT 'added sad.Investigation.ConversationId';
END
ELSE
    PRINT 'sad.Investigation.ConversationId already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Investigation_Conversation')
BEGIN
    ALTER TABLE sad.Investigation
        ADD CONSTRAINT FK_Investigation_Conversation
            FOREIGN KEY (ConversationId) REFERENCES sad.Conversation (ConversationId);
    PRINT 'added FK_Investigation_Conversation';
END
ELSE
    PRINT 'FK_Investigation_Conversation already present - skipped';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Investigation_Conversation' AND object_id = OBJECT_ID('sad.Investigation'))
BEGIN
    CREATE INDEX IX_Investigation_Conversation ON sad.Investigation (ConversationId, InvestigationId DESC);
    PRINT 'added IX_Investigation_Conversation';
END
ELSE
    PRINT 'IX_Investigation_Conversation already present - skipped';
GO

IF OBJECT_ID('sad.ConversationTurn', 'U') IS NULL
BEGIN
    CREATE TABLE sad.ConversationTurn
    (
        TurnId          BIGINT IDENTITY(1,1) NOT NULL,
        ConversationId  NVARCHAR(64)   NOT NULL,
        Role            NVARCHAR(10)   NOT NULL,
        Message         NVARCHAR(MAX)  NOT NULL,
        InvestigationId INT            NULL,
        CreatedAt       DATETIME2(3)   NOT NULL CONSTRAINT DF_ConversationTurn_CreatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_ConversationTurn PRIMARY KEY CLUSTERED (TurnId),
        CONSTRAINT FK_ConversationTurn_Conversation FOREIGN KEY (ConversationId) REFERENCES sad.Conversation (ConversationId),
        CONSTRAINT FK_ConversationTurn_Investigation FOREIGN KEY (InvestigationId) REFERENCES sad.Investigation (InvestigationId),
        CONSTRAINT CK_ConversationTurn_Role CHECK (Role IN ('User','Assistant'))
    );
    CREATE INDEX IX_ConversationTurn_Conversation ON sad.ConversationTurn (ConversationId, TurnId DESC);
    PRINT 'created sad.ConversationTurn';
END
ELSE
    PRINT 'sad.ConversationTurn already present - skipped';
GO

PRINT 'migration 002 complete';
GO
