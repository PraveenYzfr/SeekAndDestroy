"""CMDB Insighter - aggregate analytics over the ITSM/CMDB corpus.

THE ONE RULE THIS PACKAGE EXISTS TO ENFORCE
--------------------------------------------
Counting questions ("how many Sev1 incidents", "categorise them by root
cause") are never answered from retrieved text. Vector search returns the
top-k chunks most similar to the question, not all of them - "how many"
needs every matching row, not the eight that embed closest to the words in
the question. Answering a count from RAG produces a fluent, confident,
wrong number, and it is wrong in a way nothing downstream can see.

So the shape here is fixed:

    NL question
      -> LLM maps intent onto a CONSTRAINED query spec (app.insights.query_spec)
      -> Python validates the spec against a whitelist and builds parameterised
         SQL (app.insights.whitelist, app.insights.query_builder) - never
         free-form SQL from the model
      -> SQL returns exact rows
      -> LLM writes the narrative, bounded to those rows (app.insights.narrator)
      -> app.agents.guards.assert_no_number_drift, plus a per-figure fidelity
         check, before the narrative is trusted

RAG's job in this feature, if used at all, is the evidence *around* a number
- quoting a work note or a Problem.RootCause narrative - never the count
itself.
"""

from __future__ import annotations
