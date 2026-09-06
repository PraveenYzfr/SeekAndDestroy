"""The golden runner has to be able to find its own test file.

WHY THIS FILE EXISTS
--------------------
The runner lived at scripts/eval_golden.py and located its data with:

    REPO_ROOT = Path(__file__).resolve().parents[1]
    GOLDEN_SET = REPO_ROOT / "ai-service" / "app" / "evaluation" / "golden_set.yaml"

Correct from a source checkout, and impossible in the image. The container is
built from ai-service/ as its context, so it ships app/ at the root and has no
scripts/ directory at all - the script was not merely mislocated, it was absent.
Running the suite against production meant copying the file in and symlinking a
repo-shaped path, scaffolding that every container recreate wiped.

Then the file MOVED into the package and the path arithmetic came with it,
unchanged. From the new location parents[1] resolves one level differently, so
the runner pointed at /app/app/ai-service/app/evaluation/ - a directory that has
never existed anywhere. Measured on the deployed container:

    GOLDEN_SET: /app/app/ai-service/app/evaluation/golden_set.yaml
    exists:     False

AND NOTHING FAILED. `--help` still printed, because argparse never opens the
data file, so the runner looked healthy from every check anyone ran. It was
reported as done and deployed on that basis. The break would have surfaced the
next time somebody ran the suite for real - against production, with a baseline
riding on it.

These tests are the cheapest thing that turns that into a red test instead. They
assert the two properties the path must have and say nothing about how it is
computed, so a future refactor is free to change the mechanism and not the
promise.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.evaluation import golden_runner


class TestTheRunnerCanFindItsOwnData:
    def test_the_golden_set_exists(self):
        """The one assertion that would have caught both breakages. It failed
        silently for days because nothing ever opened the file in a test."""
        assert golden_runner.GOLDEN_SET.exists(), (
            f"golden_set.yaml not found at {golden_runner.GOLDEN_SET}. The runner "
            f"cannot locate its own test data - see this file's docstring."
        )

    def test_it_is_located_beside_the_runner_not_via_the_repo_layout(self):
        """The PROPERTY, not the arithmetic. The yaml has always sat beside the
        runner inside the package; asking the package where it is removes the
        repo layout from the question entirely, and is what makes the same code
        work from a checkout and from inside the image.

        A path derived from a repo root is what broke twice, so this asserts the
        relationship rather than a literal path - a future move that keeps the
        two together stays green.
        """
        assert golden_runner.GOLDEN_SET.parent == Path(golden_runner.__file__).resolve().parent

    def test_the_runner_ships_inside_the_package(self):
        """It must be importable as app.evaluation.golden_runner, because that
        is what makes `python -m app.evaluation.golden_runner` work in the image
        with no scaffolding. A runner outside the build context cannot be run
        against production at all, however correct its paths are."""
        assert golden_runner.__name__ == "app.evaluation.golden_runner"
        assert Path(golden_runner.__file__).resolve().parent.name == "evaluation"

    def test_the_data_actually_parses_and_is_not_empty(self):
        """Existence is not readability - the same distinction this codebase
        keeps elsewhere. A file that is present and unparseable fails the suite
        at case zero, which reads as a platform failure rather than a packaging
        one."""
        loaded = yaml.safe_load(golden_runner.GOLDEN_SET.read_text(encoding="utf-8"))
        cases = loaded.get("cases", loaded) if isinstance(loaded, dict) else loaded
        assert cases, "golden_set.yaml parsed to nothing"
        assert len(cases) > 1
