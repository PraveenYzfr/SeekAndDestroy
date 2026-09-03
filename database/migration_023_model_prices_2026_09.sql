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

    WHAT IS DELIBERATELY ABSENT

    groq/compound and groq/compound-mini publish no per-token price. They are
    left unpriced rather than given a plausible one - an invented number in a
    cost table is worse than a gap, because the gap is visible and the number
    is not.

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
--  The date the prices were read, used as the effective boundary. A literal
--  rather than SYSUTCDATETIME() so re-running this migration is a no-op
--  instead of opening a new price window on every deploy.
DECLARE @from DATETIME2(3) = '2026-09-04T00:00:00';

DECLARE @new TABLE (
    Provider VARCHAR(40), ModelIdentity NVARCHAR(200),
    InputPerMillion DECIMAL(12,4), OutputPerMillion DECIMAL(12,4), Notes NVARCHAR(400)
);

--  ModelIdentity is the MODEL NAME AS SENT TO THE PROVIDER, because that is
--  what audit_repository looks up - deliberately not the "provider:model"
--  identity it stores. For Groq the model name genuinely contains a slash
--  ("openai/gpt-oss-120b"); that is the model id, not a provider prefix.
INSERT INTO @new (Provider, ModelIdentity, InputPerMillion, OutputPerMillion, Notes) VALUES
    -- api-docs.deepseek.com/quick_start/pricing - peak, cache-miss (see header)
    ('deepseek',  'deepseek-v4-flash',       0.4400,  1.3200, 'api-docs.deepseek.com 2026-09-04; peak rate, cache miss; off-peak is half'),
    ('deepseek',  'deepseek-v4-pro',         1.3200,  3.9600, 'api-docs.deepseek.com 2026-09-04; peak rate, cache miss; off-peak is half'),

    -- console.groq.com/docs/models
    ('groq',      'openai/gpt-oss-120b',     0.1500,  0.6000, 'console.groq.com/docs/models 2026-09-04'),
    ('groq',      'openai/gpt-oss-20b',      0.0750,  0.3000, 'console.groq.com/docs/models 2026-09-04'),
    ('groq',      'qwen/qwen3.6-27b',        0.6000,  3.0000, 'console.groq.com/docs/models 2026-09-04; preview model'),
    ('groq',      'qwen/qwen3.8-27b',        0.8000,  4.0000, 'console.groq.com/docs/models 2026-09-04; preview model'),

    -- ai.google.dev/gemini-api/docs/pricing - text rates
    ('gemini',    'gemini-3.5-flash',        1.5000,  9.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04'),
    ('gemini',    'gemini-3.5-flash-lite',   0.3000,  2.5000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; judge model'),
    ('gemini',    'gemini-3.1-flash-lite',   0.2500,  1.5000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; text rate, audio is 0.50'),
    ('gemini',    'gemini-2.5-flash',        0.3000,  2.5000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04'),
    ('gemini',    'gemini-2.5-flash-lite',   0.1000,  0.4000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04'),
    --  CORRECTION: seeded as free in migration_014. It is not free.
    ('gemini',    'gemini-embedding-001',    0.1500,  0.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; CORRECTS the 0.00 free-tier row in migration_014; embeddings have no output tokens'),
    ('gemini',    'gemini-embedding-2',      0.2000,  0.0000, 'ai.google.dev/gemini-api/docs/pricing 2026-09-04; text rate'),

    -- claude.com/pricing
    ('anthropic', 'claude-opus-5',           5.0000, 25.0000, 'claude.com/pricing 2026-09-04'),
    ('anthropic', 'claude-sonnet-5',         2.0000, 10.0000, 'claude.com/pricing 2026-09-04'),
    ('anthropic', 'claude-haiku-4-5',        1.0000,  5.0000, 'claude.com/pricing 2026-09-04'),
    ('anthropic', 'claude-fable-5-1',       10.0000, 50.0000, 'claude.com/pricing 2026-09-04');

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

PRINT CONCAT('model prices current as of 2026-09-04: ', @@ROWCOUNT, ' row(s) inserted');
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
