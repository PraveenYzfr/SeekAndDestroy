/*  Migration 003 - per-call token accounting on sad.AgentAuditLog.

    The audit row already records which model was asked what, and whether it
    answered. It did not record how much that cost, because HttpChatModel
    discarded the `usage` block every OpenAI-compatible provider returns and
    GeminiChatModel discarded `usageMetadata`.

    That gap mattered more here than it would elsewhere: this platform holds
    four provider keys specifically to compare them, and "which model is
    cheaper for narration" is unanswerable without token counts. A call
    counter tells you how often; only tokens tell you how much - and the
    difference between providers is large. A reasoning model can spend
    thousands of tokens deliberating before writing a 400-token answer, which
    is invisible in a call count and obvious in a token count.

    Nullable on purpose:
      - the mock model reports no usage,
      - a cache hit consumes no tokens,
      - a provider may omit the block,
      - and every row written before this migration has none.
    NULL means "not recorded", which is different from zero and must stay
    distinguishable, or an average cost per call quietly counts cache hits as
    free real calls.

    Idempotent, like migrations 001 and 002: re-running is a no-op.
*/

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sad.AgentAuditLog') AND name = 'PromptTokens'
)
BEGIN
    ALTER TABLE sad.AgentAuditLog ADD PromptTokens INT NULL;
    PRINT 'added sad.AgentAuditLog.PromptTokens';
END
ELSE
    PRINT 'sad.AgentAuditLog.PromptTokens already present - skipped';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sad.AgentAuditLog') AND name = 'CompletionTokens'
)
BEGIN
    ALTER TABLE sad.AgentAuditLog ADD CompletionTokens INT NULL;
    PRINT 'added sad.AgentAuditLog.CompletionTokens';
END
ELSE
    PRINT 'sad.AgentAuditLog.CompletionTokens already present - skipped';
GO

/*  Model identity is already inside InputJson, but only as free text inside a
    serialised payload. A column makes "tokens by model" a GROUP BY rather than
    a JSON parse over every row, which is the query this table exists to
    answer once the evaluation harness runs across providers.
*/
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sad.AgentAuditLog') AND name = 'ModelIdentity'
)
BEGIN
    ALTER TABLE sad.AgentAuditLog ADD ModelIdentity NVARCHAR(200) NULL;
    PRINT 'added sad.AgentAuditLog.ModelIdentity';
END
ELSE
    PRINT 'sad.AgentAuditLog.ModelIdentity already present - skipped';
GO
