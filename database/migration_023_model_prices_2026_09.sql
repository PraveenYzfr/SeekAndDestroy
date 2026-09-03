/*  Real prices, for the models this platform actually calls.

    WHY THIS EXISTS

    migration_014 seeded twelve prices in January and none of them is a model
    we run any more:

        seeded                    actually configured (app/config/settings.py)
        ------------------------  ------------------------------------------
        gemini-2.0-flash          gemini-3.5-flash-lite   (judge)
        deepseek-chat             deepseek-v4-flash       (extraction, reporting)
        deepseek-reasoner         deepseek-v4-pro         (summarisation, qa)
        llama-3.3-70b-versatile   openai/gpt-oss-120b     (costly tier)
        gpt-4o                    openai/gpt-oss-20b      (cheap tier)

    app/repositories/audit_repository.py prices a call by looking the model up
    here. A model with no row is recorded UNPRICED - honestly, and the daily
    spend view counts those separately rather than as zero - but the effect was
    that sad_llm_cost_usd_total and every spend guard saw almost nothing, on a
    platform making real billed calls all day.

    WHERE THE NUMBERS COME FROM

    Each vendor's own published pricing page, read on 2026-09-04, with the URL
    in the Notes column so a figure can be checked rather than trusted. Nothing
    here is recalled or estimated.

    THE THREE JUDGEMENT CALLS, ALL MADE THE SAME WAY

    This table has ONE input price per model, and two vendors quote more than
    one. Where a choice was needed, the HIGHER figure wins, because these
    numbers feed a spend budget: a guard that under-states cost fails open, and
    finds out at the invoice.

      1. DeepSeek publishes peak and off-peak, off-peak being half. Peak taken.
      2. DeepSeek publishes cache-hit and cache-miss input prices, a 30x
         difference. Cache-miss taken. Real spend will usually be lower.
      3. Gemini quotes a text rate and a higher audio rate on the lite models.
         This platform sends text, so the text rate is the right one - the
         exception to the rule above, and it is not a hedge: charging ourselves
         the audio rate for text would overstate every call.

    COVERAGE: EVERY MODEL EACH VENDOR PUBLISHES A PRICE FOR

    79 rows, which is the whole of each provider's public price list, not just
    the six models currently assigned to a role. The Model Settings dropdown is
    built from each provider's LIVE /models listing (app/agents/providers.py),
    so any of them can be selected at any time, and a model that becomes
    unpriced the moment somebody picks it from a dropdown is a spend total that
    silently stops counting.

    Dated snapshots are NOT listed. A provider serves claude-haiku-4-5 as
    claude-haiku-4-5-20251001 and gpt-5-nano as gpt-5-nano-2025-08-07, and the
    dropdown shows the dated id. Enumerating those here would go stale weekly.
    app/services/model_pricing._dated_snapshot_rows prices them from their base
    model's row by longest-prefix match instead - so a new snapshot of a known
    model is priced on the day it appears, with no migration.

    WHAT IS DELIBERATELY ABSENT

    groq/compound and groq/compound-mini publish no per-token price, and Groq's
    llama models say "Contact Sales". They are left unpriced rather than given
    a plausible one - an invented number in a cost table is worse than a gap,
    because the gap is visible and the number is not. The query at the end of
    this file names any unpriced model actually seen in the audit log, so the
    gap stays visible rather than being discovered at the invoice.

    ONE CORRECTION, NOT AN ADDITION

    gemini-embedding-001 was seeded at 0.0000/0.0000 with the note "free tier,
    shared quota pool". It is $0.15 per million input tokens on the paid tier.
    Every embedding this platform has billed has been recorded as free.

    EFFECTIVE-DATED, NOT OVERWRITTEN

    The old rows are closed, not deleted. A call made in June was billed at
    June's price and must still price that way when the audit log is re-read -
    which is exactly what EffectiveFrom/EffectiveTo are for, and price_for(at=)
    already honours.
*/

SET NOCOUNT ON;
GO

--  ONE BATCH, because the guard has to cover the work.
--
--  The first version put the existence check in its own batch ending in
--  RETURN. RETURN exits the BATCH, not the script, so on a database without
--  sad.ModelPrice the guard would print its message and the next batch would
--  run anyway and fail on the missing table - a guard that reports the problem
--  and then walks into it.
IF OBJECT_ID('sad.ModelPrice', 'U') IS NULL
    PRINT 'sad.ModelPrice does not exist - run migration_014 first; skipped';
ELSE
BEGIN
--  The effective boundary. A literal rather than SYSUTCDATETIME() so
--  re-running this migration is a no-op instead of opening a new price window
--  on every deploy.
--
--  IN UTC, AND THAT IS THE WHOLE POINT OF THIS COMMENT. The first version used
--  '2026-09-04' because that was the local date where it was written - India,
--  UTC+5:30. UTC was still 2026-09-03T21:00, so every row was inserted with a
--  window that opened THREE HOURS IN THE FUTURE. price_for filters on
--  EffectiveFrom <= now, so all 79 prices loaded, the table looked right, and
--  every single call still recorded UNPRICED. Caught only by pricing a real
--  investigation and finding 0 of 8 priced - the table having rows in it
--  proved nothing.
--
--  So the boundary is the start of the UTC day the prices were read, which is
--  already past by construction, and the guard below refuses to leave a window
--  open in the future.
DECLARE @from DATETIME2(3) = '2026-09-03T00:00:00';

DECLARE @new TABLE (
    Provider VARCHAR(40), ModelIdentity NVARCHAR(200),
    InputPerMillion DECIMAL(12,4), OutputPerMillion DECIMAL(12,4), Notes NVARCHAR(400)
);

--  ModelIdentity is the MODEL NAME AS SENT TO THE PROVIDER, because that is
--  what audit_repository looks up - deliberately not the "provider:model"
--  identity it stores. For Groq the model name genuinely contains a slash
--  ("openai/gpt-oss-120b"); that is the model id, not a provider prefix.
INSERT INTO @new (Provider, ModelIdentity, InputPerMillion, OutputPerMillion, Notes) VALUES
    ('deepseek', 'deepseek-v4-flash', 0.4400, 1.3200, 'api-docs.deepseek.com 2026-09-04; peak rate, cache miss; off-peak is half'),
    ('deepseek', 'deepseek-v4-pro', 1.3200, 3.9600, 'api-docs.deepseek.com 2026-09-04; peak rate, cache miss; off-peak is half'),
    ('deepseek', 'deepseek-v4-flash-vision-exp', 0.4400, 1.3200, 'api-docs.deepseek.com 2026-09-04; peak rate, cache miss'),
    ('groq', 'openai/gpt-oss-120b', 0.1500, 0.6000, 'console.groq.com/docs/models 2026-09-04; costly tier'),
    ('groq', 'openai/gpt-oss-20b', 0.0750, 0.3000, 'console.groq.com/docs/models 2026-09-04; cheap tier'),
    ('groq', 'qwen/qwen3.6-27b', 0.6000, 3.0000, 'console.groq.com/docs/models 2026-09-04; preview model'),
    ('groq', 'qwen/qwen3.8-27b', 0.8000, 4.0000, 'console.groq.com/docs/models 2026-09-04; preview model'),
    ('gemini', 'gemini-3.8-flash', 0.7500, 3.7500, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; promotional through 2026-12-31; 1.50/7.50 after'),
    ('gemini', 'gemini-3.7-flash', 0.7500, 3.7500, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; promotional through 2026-12-31; 1.50/7.50 after'),
    ('gemini', 'gemini-3.6-flash', 0.7500, 3.7500, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; promotional through 2026-12-31; 1.50/7.50 after'),
    ('gemini', 'gemini-3.5-flash', 1.5000, 9.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04'),
    ('gemini', 'gemini-3.5-flash-lite', 0.3000, 2.5000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; judge model'),
    ('gemini', 'gemini-3.1-flash-lite', 0.2500, 1.5000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; text rate; audio is 0.50'),
    ('gemini', 'gemini-3.1-pro-preview', 2.0000, 12.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; prompts <=200k; 4.00/18.00 above'),
    ('gemini', 'gemini-2.5-pro', 1.2500, 10.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; prompts <=200k; 2.50/15.00 above'),
    ('gemini', 'gemini-2.5-flash', 0.3000, 2.5000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; text rate; audio is 1.00'),
    ('gemini', 'gemini-2.5-flash-lite', 0.1000, 0.4000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; text rate; audio is 0.30'),
    ('gemini', 'gemini-embedding-001', 0.1500, 0.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; CORRECTS the 0.00 free-tier row in migration_014'),
    ('gemini', 'gemini-embedding-2', 0.2000, 0.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; text rate'),
    ('anthropic', 'claude-fable-5-1', 10.0000, 50.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-fable-5', 10.0000, 50.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-mythos-5-1', 10.0000, 50.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04; limited availability'),
    ('anthropic', 'claude-mythos-5', 10.0000, 50.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04; limited availability'),
    ('anthropic', 'claude-opus-5', 5.0000, 25.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-opus-4-8', 5.0000, 25.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-opus-4-7', 5.0000, 25.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-opus-4-6', 5.0000, 25.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-opus-4-5', 5.0000, 25.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-opus-4-1', 15.0000, 75.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04; retired except on Bedrock and Google Cloud'),
    ('anthropic', 'claude-opus-4', 15.0000, 75.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04; retired except on Google Cloud'),
    ('anthropic', 'claude-sonnet-5', 2.0000, 10.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04; introductory 2/10 is now the standard price'),
    ('anthropic', 'claude-sonnet-4-6', 3.0000, 15.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-sonnet-4-5', 3.0000, 15.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-sonnet-4', 3.0000, 15.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04; retired except on Bedrock and Google Cloud'),
    ('anthropic', 'claude-haiku-4-5', 1.0000, 5.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04'),
    ('anthropic', 'claude-haiku-3-5', 0.8000, 4.0000, 'platform.claude.com/docs/en/about-claude/pricing 2026-09-04; retired except on Bedrock and Google Cloud'),
    ('openai', 'gpt-6-astra', 10.0000, 50.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.6-sol', 4.0000, 20.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.6-terra', 2.0000, 12.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.6-luna', 0.2000, 1.2000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.6-cyber', 12.5000, 75.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.5', 5.0000, 30.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.5-pro', 30.0000, 180.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.5-cyber', 12.5000, 75.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.4', 2.5000, 15.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.4-mini', 0.7500, 4.5000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.4-nano', 0.2000, 1.2500, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.4-pro', 30.0000, 180.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.3-codex', 1.7500, 14.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.2', 1.7500, 14.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.2-pro', 21.0000, 168.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5.1', 1.2500, 10.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5', 1.2500, 10.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5-mini', 0.2500, 2.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5-nano', 0.0500, 0.4000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5-pro', 15.0000, 120.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-5-search-api', 1.2500, 10.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-4.1', 2.0000, 8.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-4.1-mini', 0.4000, 1.6000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-4.1-nano', 0.1000, 0.4000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-4o', 2.5000, 10.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-4o-2024-05-13', 5.0000, 15.0000, 'developers.openai.com/api/docs/pricing 2026-09-04; the dated snapshot is dearer than the base model'),
    ('openai', 'gpt-4o-mini', 0.1500, 0.6000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'chat-latest', 5.0000, 30.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'o1', 15.0000, 60.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'o1-pro', 150.0000, 600.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'o3', 2.0000, 8.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'o3-pro', 20.0000, 80.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'o3-mini', 1.1000, 4.4000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'o4-mini', 1.1000, 4.4000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-4-turbo-2024-04-09', 10.0000, 30.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-4-0613', 30.0000, 60.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-3.5-turbo', 0.5000, 1.5000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-3.5-turbo-0125', 0.5000, 1.5000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-3.5-turbo-1106', 1.0000, 2.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'gpt-3.5-turbo-instruct', 1.5000, 2.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'text-embedding-3-small', 0.0200, 0.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'text-embedding-3-large', 0.1300, 0.0000, 'developers.openai.com/api/docs/pricing 2026-09-04'),
    ('openai', 'text-embedding-ada-002', 0.1000, 0.0000, 'developers.openai.com/api/docs/pricing 2026-09-04');

--  Close only the rows whose price actually CHANGED. Re-running must not open
--  a new window that says the same thing as the one it replaced: a price
--  history full of identical adjacent rows cannot answer "when did this
--  change", which is the only question it exists to answer.
UPDATE p
SET    EffectiveTo = @from
FROM   sad.ModelPrice p
JOIN   @new n ON n.ModelIdentity = p.ModelIdentity
WHERE  p.EffectiveTo IS NULL
  AND  p.EffectiveFrom < @from
  AND  (p.InputPerMillion <> n.InputPerMillion OR p.OutputPerMillion <> n.OutputPerMillion);

INSERT INTO sad.ModelPrice (Provider, ModelIdentity, InputPerMillion, OutputPerMillion, Currency, EffectiveFrom, Notes)
SELECT n.Provider, n.ModelIdentity, n.InputPerMillion, n.OutputPerMillion, 'USD', @from, n.Notes
FROM   @new n
WHERE  NOT EXISTS (
    SELECT 1 FROM sad.ModelPrice p
    WHERE  p.ModelIdentity = n.ModelIdentity
      AND  p.EffectiveTo IS NULL
      AND  p.InputPerMillion = n.InputPerMillion
      AND  p.OutputPerMillion = n.OutputPerMillion
);

DECLARE @inserted INT = @@ROWCOUNT;

--  REPAIR, for a database that already ran the version with the future date.
--  Those rows are present, current, and invisible to price_for until the
--  window opens. Re-running the insert above will not fix them - the
--  NOT EXISTS sees a current row at the right price and correctly skips it -
--  so the boundary itself has to be moved.
UPDATE sad.ModelPrice
SET    EffectiveFrom = @from
WHERE  EffectiveFrom > SYSUTCDATETIME();

DECLARE @unfuture INT = @@ROWCOUNT;

--  And the matching half: a row closed AT that future boundary would leave a
--  gap where the old price has ended and the new one has not begun, so a call
--  in the gap prices as unknown rather than as either price.
UPDATE sad.ModelPrice
SET    EffectiveTo = @from
WHERE  EffectiveTo > SYSUTCDATETIME()
  AND  EffectiveTo > EffectiveFrom
  AND  EffectiveFrom < @from;

PRINT CONCAT('model prices effective ', CONVERT(VARCHAR(10), @from, 120), ': ',
             @inserted, ' inserted, ', @unfuture, ' future-dated row(s) corrected');

--  A price nobody can see is worse than a price nobody loaded, because the
--  table looks correct. Say so rather than leaving it to a spend report.
IF EXISTS (SELECT 1 FROM sad.ModelPrice WHERE EffectiveTo IS NULL AND EffectiveFrom > SYSUTCDATETIME())
    PRINT 'WARNING: prices exist whose window has not opened yet - they will not price any call';
END
GO

--  WHAT IS STILL UNPRICED, said out loud rather than left to be discovered.
--  A configured model with no price is not an error - the platform records
--  those calls as cost-unknown by design - but it should be a deliberate gap,
--  not a surprise.
IF OBJECT_ID('sad.AgentAuditLog', 'U') IS NOT NULL
SELECT DISTINCT 'UNPRICED MODEL SEEN IN THE AUDIT LOG: ' + a.ModelIdentity AS Warning
FROM   sad.AgentAuditLog a
WHERE  a.ModelIdentity IS NOT NULL
  AND  a.ModelIdentity <> ''
  AND  NOT EXISTS (
        SELECT 1 FROM sad.ModelPrice p
        WHERE  p.EffectiveTo IS NULL
          AND  (p.ModelIdentity = a.ModelIdentity
                OR a.ModelIdentity LIKE '%:' + p.ModelIdentity)
  );
GO
