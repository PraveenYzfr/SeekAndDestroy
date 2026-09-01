# Work in flight

Three Claude sessions share this working tree. Committed work is attributed by the
`Agent:` trailer in the commit message; this file covers work that is NOT yet
committed, which is where the attribution gap actually bites.

Append-only. One line per claim, filenames not prose. If two sessions append at
once, keep both lines - that is why it is append-only rather than edited in place.
Claims are advisory: they say "I am in here", not "you may not enter". Announce in
chat before touching a file someone else has claimed.

| Agent | Files | Work |
|-------|-------|------|
| e7 | `scripts/generate_seed.py` `scripts/seed_cmdb.py` `database/*.sql` | CMDB generator |
| ef | `ai-service/app/scoring/*` `ai-service/app/rules/*` `ai-service/app/repositories/ci_*` | Resiliency + SPOF on the CI graph |
| c2 | `ai-service/app/insights/*` `ai-service/tests/test_insights.py` | CMDB Insighter |
