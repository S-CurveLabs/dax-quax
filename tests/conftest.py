from __future__ import annotations

import json
import pathlib

import pytest

from dax_quax.sources import dmv

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PBIP = FIXTURES / "synthetic_pbip"


def load_rowsets(name: str) -> dmv.Rowsets:
    data = json.loads((FIXTURES / name / "rowsets.json").read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


@pytest.fixture
def synthetic_rowsets() -> dmv.Rowsets:
    return load_rowsets("synthetic")


@pytest.fixture
def synthetic_model(synthetic_rowsets):
    return dmv.build_model(synthetic_rowsets, name="Synthetic", source="live")


@pytest.fixture
def metadata_only_model(synthetic_rowsets):
    """Same model with every storage rowset withheld — the PBIP-without-cache shape."""
    trimmed = {k: v for k, v in synthetic_rowsets.items() if k not in dmv.METRIC_QUERIES}
    return dmv.build_model(trimmed, name="Synthetic (metadata only)", source="pbip")


@pytest.fixture
def synthetic_report():
    from dax_quax.report import load_report

    return load_report(PBIP)


@pytest.fixture
def model_with_report(synthetic_rowsets, synthetic_report):
    """A *separate* model instance.

    attach_report mutates the model it is given, so reusing synthetic_model here would
    hand both this fixture and that one the same already-attached object, and any test
    comparing the with- and without-report views would silently compare a thing to itself.
    """
    import copy

    from dax_quax.report import attach_report

    model = dmv.build_model(synthetic_rowsets, name="Synthetic", source="live")
    return attach_report(model, copy.deepcopy(synthetic_report))


@pytest.fixture
def pbip_model():
    """The same logical model as the rowsets fixture, loaded from TMDL instead."""
    from dax_quax.sources.pbip import load_model

    return load_model(PBIP)


RICH = FIXTURES / "rich_pbip"


@pytest.fixture
def rich_model():
    """A model carrying every object kind: roles, hierarchies, calculation groups,
    detail rows. Kept apart from `synthetic_model`, whose numbers other tests depend on."""
    from dax_quax.sources.pbip import load_model

    return load_model(RICH)


@pytest.fixture
def rich_rowsets():
    return load_rowsets("rich")


@pytest.fixture
def rich_live_model(rich_rowsets):
    """The same logical model as `rich_model`, from DMV rowsets instead of TMDL."""
    return dmv.build_model(rich_rowsets, name="Rich", source="live")
