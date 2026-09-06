#!/usr/bin/env python
"""Run the golden suite from a source checkout.

The runner itself lives in the PACKAGE, at app/evaluation/golden_runner.py, so
that it ships inside the image and can be run against production with:

    docker exec -w /app docker-ai-service-1 python -m app.evaluation.golden_runner

It used to live here, and here is outside the Docker build context - the image is
built from ai-service/ and has no scripts/ directory at all. So the suite could
not run inside the deployed image, and the workaround was to copy the script in
and symlink a repo-shaped path on every deploy, scaffolding that the next
container recreate wiped. "Run the golden suite on prod" sat unstarted for days
without ever being the thing that blocked anyone.

This file stays as a thin wrapper because `python scripts/eval_golden.py` is in
people's shell history and in the docs, and breaking that to fix a packaging
problem would trade one papercut for another. It adds nothing but the import
path a checkout needs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-service"))

from app.evaluation.golden_runner import main  # noqa: E402  - after the path insert

if __name__ == "__main__":
    sys.exit(main())
