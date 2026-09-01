/*  Migration 014 - what a model call actually cost.

    WHAT WAS MISSING
    ----------------
    sad.AgentAuditLog records PromptTokens, CompletionTokens and ModelIdentity for
    every call. Nothing converts them to money. There is no price table, no cost
    column, and no code that multiplies one by the other.

    So "what did this investigation cost" and "which model is cheaper for the same
    evidence" are unanswerable, and the daily budget enforces a ceiling in CALLS -
    50,000 of them - which treats a 200-token classification and a 40,000-token
    report as the same unit of spend. A single expensive model could exhaust a
    rupee budget while sitting at 2% of its call budget.

    PRICE IS STORED, NOT DERIVED
    ---------------------------
    UnitPriceInput and UnitPriceOutput are written onto the call row at the moment
    the call happens, and CostUsd is computed from them there.

    That is deliberate and it is the part most likely to be "simplified" later.
    Re-deriving historical cost by joining to a current price table gives the wrong
    answer the first time a vendor changes pricing: every call ever made silently
    re-prices, last quarter's spend changes, and the number that was reported to
    somebody stops matching the number in the database. A cost is a fact about an
    event, not a property of a model.

    The price table is therefore effective-dated and only ever consulted when a
    call is made. Old rows keep the price they were charged.
*/

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

-- =============================================================================
-- 1. Prices, effective-dated
-- =============================================================================
IF OBJECT_ID('sad.ModelPrice', 'U') IS NULL
BEGIN
    CREATE TABLE sad.ModelPrice
    (
        ModelPriceId     INT IDENTITY(1,1) NOT NULL,
        Provider         VARCHAR(40)       NOT NULL,
        ModelIdentity    NVARCHAR(200)     NOT NULL,
        -- Per MILLION tokens, which is how every provider quotes. Storing per
        -- token invites a float with six leading zeros and the rounding errors
        -- that come with it.
        InputPerMillion  DECIMAL(12,4)     NOT NULL,
        OutputPerMillion DECIMAL(12,4)     NOT NULL,
        Currency         CHAR(3)           NOT NULL CONSTRAINT DF_ModelPrice_Currency DEFAULT ('USD'),
        EffectiveFrom    DATETIME2(3)      NOT NULL,
        EffectiveTo      DATETIME2(3)      NULL,      -- NULL = still current
        Notes            NVARCHAR(400)     NULL,
        CONSTRAINT PK_ModelPrice PRIMARY KEY CLUSTERED (ModelPriceId),
        CONSTRAINT CK_ModelPrice_Window CHECK (EffectiveTo IS NULL OR EffectiveTo > EffectiveFrom),
        CONSTRAINT CK_ModelPrice_NonNegative CHECK (InputPerMillion >= 0 AND OutputPerMillion >= 0)
    );
    CREATE INDEX IX_ModelPrice_Lookup ON sad.ModelPrice (ModelIdentity, EffectiveFrom) INCLUDE (InputPerMillion, OutputPerMillion, Currency);
    PRINT 'created sad.ModelPrice';
END
ELSE PRINT 'sad.ModelPrice already present - skipped';
GO

-- =============================================================================
-- 2. Cost on the call row
-- =============================================================================
IF COL_LENGTH('sad.AgentAuditLog', 'CostUsd') IS NULL
BEGIN
    ALTER TABLE sad.AgentAuditLog ADD
        CostUsd           DECIMAL(12,6) NULL,   -- computed at call time
        UnitPriceInput    DECIMAL(12,4) NULL,   -- the price in force THEN
        UnitPriceOutput   DECIMAL(12,4) NULL,
        Provider          VARCHAR(40)   NULL,
        LatencyMs         INT           NULL,
        CacheHit          BIT           NULL,
        FellBackFrom      VARCHAR(40)   NULL;
    PRINT 'added cost and latency columns to sad.AgentAuditLog';
END
ELSE PRINT 'sad.AgentAuditLog cost columns already present - skipped';
GO

-- StartedAt/CompletedAt already exist, so latency was computable and never
-- computed. LatencyMs is stored because a percentile over 90 days of DATEDIFF on
-- two DATETIME2 columns is a table scan, and the question "is this model slower"
-- should not cost a scan every time somebody asks it.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AgentAuditLog_Model')
   AND COL_LENGTH('sad.AgentAuditLog', 'CostUsd') IS NOT NULL
BEGIN
    CREATE INDEX IX_AgentAuditLog_Model ON sad.AgentAuditLog (ModelIdentity, StartedAt)
        INCLUDE (CostUsd, LatencyMs, PromptTokens, CompletionTokens);
    PRINT 'created IX_AgentAuditLog_Model';
END
GO

-- =============================================================================
-- 3. Published prices as of 2 September 2026
-- =============================================================================
-- Seeded rather than left empty so cost reporting works on day one instead of
-- silently returning NULL for every call. Wrong-but-stated beats absent: a figure
-- somebody can correct is more useful than a blank nobody notices.
--
-- EffectiveFrom is deliberately in the past so calls already recorded can be
-- priced retrospectively where their tokens are known.
IF NOT EXISTS (SELECT 1 FROM sad.ModelPrice)
BEGIN
    INSERT INTO sad.ModelPrice (Provider, ModelIdentity, InputPerMillion, OutputPerMillion, Currency, EffectiveFrom, Notes)
    VALUES
        ('gemini',   'gemini-2.0-flash',          0.1000,  0.4000, 'USD', '2026-01-01', 'published list price'),
        ('gemini',   'gemini-2.5-pro',            1.2500, 10.0000, 'USD', '2026-01-01', 'published list price'),
        ('gemini',   'text-embedding-004',        0.0000,  0.0000, 'USD', '2026-01-01', 'free tier, shared quota pool'),
        ('gemini',   'gemini-embedding-001',      0.0000,  0.0000, 'USD', '2026-01-01', 'free tier, shared quota pool'),
        ('deepseek', 'deepseek-chat',             0.2700,  1.1000, 'USD', '2026-01-01', 'cost-first default'),
        ('deepseek', 'deepseek-reasoner',         0.5500,  2.1900, 'USD', '2026-01-01', NULL),
        ('groq',     'llama-3.3-70b-versatile',   0.5900,  0.7900, 'USD', '2026-01-01', NULL),
        ('openai',   'gpt-4o',                    2.5000, 10.0000, 'USD', '2026-01-01', 'benchmark only'),
        ('openai',   'gpt-4o-mini',               0.1500,  0.6000, 'USD', '2026-01-01', NULL),
        ('anthropic','claude-sonnet-4',           3.0000, 15.0000, 'USD', '2026-01-01', 'benchmark only'),
        ('anthropic','claude-haiku-4-5',          1.0000,  5.0000, 'USD', '2026-01-01', NULL),
        ('mock',     'mock',                      0.0000,  0.0000, 'USD', '2026-01-01', 'offline development only');
    PRINT 'seeded 12 model prices';
END
ELSE PRINT 'sad.ModelPrice already populated - skipped';
GO

-- =============================================================================
-- 4. Spend, readable without a join every time
-- =============================================================================
-- A view rather than a rollup table: spend has to be correct to the last call for
-- a budget to mean anything, and a materialised total is a second thing that can
-- disagree with the first.
IF OBJECT_ID('sad.ModelSpendDaily', 'V') IS NOT NULL
    DROP VIEW sad.ModelSpendDaily;
GO

CREATE VIEW sad.ModelSpendDaily
AS
SELECT
    CAST(a.StartedAt AS DATE)                       AS SpendDate,
    ISNULL(a.Provider, 'unknown')                   AS Provider,
    ISNULL(a.ModelIdentity, 'unknown')              AS ModelIdentity,
    COUNT(*)                                        AS Calls,
    SUM(ISNULL(a.PromptTokens, 0))                  AS PromptTokens,
    SUM(ISNULL(a.CompletionTokens, 0))              AS CompletionTokens,
    SUM(ISNULL(a.CostUsd, 0))                       AS CostUsd,
    -- Calls whose cost is unknown are reported, not hidden. A spend figure that
    -- silently omits unpriced calls reads as authoritative and is not.
    SUM(CASE WHEN a.CostUsd IS NULL THEN 1 ELSE 0 END) AS UnpricedCalls,
    AVG(CAST(a.LatencyMs AS FLOAT))                 AS AvgLatencyMs,
    MAX(a.LatencyMs)                                AS MaxLatencyMs
FROM   sad.AgentAuditLog a
GROUP BY CAST(a.StartedAt AS DATE), ISNULL(a.Provider, 'unknown'), ISNULL(a.ModelIdentity, 'unknown');
GO

PRINT '--- migration 014 complete ---';
GO
