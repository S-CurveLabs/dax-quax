"""The packaging check, and the two ways it can rot.

A stray top-level import of an optional package makes the whole library unimportable for
anyone who did not install that extra, and it is invisible on a machine that has them all.
This is the check for that, so it needs its own.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "tools"))

from check_imports import NEEDS_EXTRA, analyse, probe  # noqa: E402


def test_every_module_imports_with_no_extras():
    """The check itself, run for real. It is fast enough to be an ordinary test."""
    assert analyse(probe()) == []


def test_the_real_outcome_covers_the_whole_package():
    outcome = probe()
    assert len(outcome) > 30
    assert "dax_quax.sources.live" in outcome
    assert "dax_quax.render.serve" in outcome


def test_live_is_checked_rather_than_skipped():
    """It was skipped as "Windows-only" while importing perfectly well without pythonnet.

    A top-level pythonnet import in it would have sailed past the job that exists to catch
    exactly that, so the skip is gone and the module is checked like any other.
    """
    assert "dax_quax.sources.live" not in NEEDS_EXTRA
    assert probe()["dax_quax.sources.live"] is None


# -- the two ways it rots ------------------------------------------------------------


def test_a_stray_top_level_import_is_caught():
    outcome = {"dax_quax.analysis.usage": "ModuleNotFoundError: No module named 'pbixray'"}
    problems = analyse(outcome, allowances={})
    assert problems
    assert "pbixray" in problems[0]


def test_a_stale_allowance_is_caught():
    """If a module stops needing its extra, the exception must go, not linger."""
    problems = analyse({"m": None}, allowances={"m": "fastapi"})
    assert problems
    assert "stale" in problems[0]


def test_an_allowance_does_not_excuse_a_different_failure():
    """Allowed to need fastapi is not allowed to be broken."""
    problems = analyse({"m": "SyntaxError: bad"}, allowances={"m": "fastapi"})
    assert problems
    assert "different problem" in problems[0]


def test_an_allowance_for_a_module_that_does_not_exist_is_caught():
    problems = analyse({}, allowances={"dax_quax.gone": "fastapi"})
    assert problems
    assert "no such module" in problems[0]


def test_a_clean_outcome_has_no_problems():
    assert analyse({"a": None, "b": "ImportError: fastapi"}, allowances={"b": "fastapi"}) == []
