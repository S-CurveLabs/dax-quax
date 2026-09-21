"""Workspace scan.

The fixture holds every case: a model with two reports over it, a model matched by
connection name, a model nothing references, and a report pointing at a dataset that is not
here. The test that matters most is `test_a_second_report_rescues_a_column`.
"""

from __future__ import annotations

import json

import pytest

from dax_quax.analysis.usage import Verdict, assess
from dax_quax.errors import DaxQuaxError
from dax_quax.sources.workspace import discover_workspace

WS = "tests/fixtures/synthetic_workspace"
SINGLE = "tests/fixtures/synthetic_pbip"


@pytest.fixture
def workspace():
    return discover_workspace(WS)


# -- discovery ------------------------------------------------------------------------------


def test_finds_every_artefact(workspace):
    assert set(workspace.model_paths) == {"Contoso", "Regional", "Orphan"}
    assert {link.name for link in workspace.links} == {
        "Contoso.Report",
        "Exec.Report",
        "Regional.Report",
        "Stray.Report",
    }


def test_by_path_links_to_a_sibling_model(workspace):
    link = next(x for x in workspace.links if x.name == "Exec.Report")
    assert link.by_path == "../Contoso.SemanticModel"
    assert link.model == "Contoso"


def test_by_connection_matches_on_the_dataset_name(workspace):
    link = next(x for x in workspace.links if x.name == "Regional.Report")
    assert link.by_connection == "Regional"
    assert link.model == "Regional"


def test_two_reports_can_share_one_model(workspace):
    assert {link.name for link in workspace.reports_for("Contoso")} == {
        "Contoso.Report",
        "Exec.Report",
    }


def test_an_unmatched_report_says_why(workspace):
    stray = next(x for x in workspace.links if x.name == "Stray.Report")
    assert not stray.matched
    assert "Warehouse Finance" in stray.reason
    assert "published only, or renamed" in stray.reason


def test_orphan_models(workspace):
    """A model nothing references is often a bigger win than any single column."""
    assert workspace.orphan_models() == ["Orphan"]


def test_describe_is_the_coverage_statement(workspace):
    assert workspace.describe() == "3 model(s), 4 report(s), 3 matched, 1 unmatched"


def test_load_can_be_skipped():
    lightweight = discover_workspace(WS, load=False)
    assert lightweight.models == {}
    assert len(lightweight.links) == 4


def test_a_missing_directory_is_an_error(tmp_path):
    with pytest.raises(DaxQuaxError, match="not a directory"):
        discover_workspace(tmp_path / "nope")


# -- THE POINT OF PHASE 2 ------------------------------------------------------------------------


def test_a_second_report_rescues_a_column():
    """Sales[Order Number] is unused by Contoso.Report and used by Exec.Report.

    Assessed against one report it is REMOVE, and deleting it breaks the other report.
    This is the failure the whole workspace scan exists to prevent.
    """
    from dax_quax.sources.pbip import open_pbip

    alone = {f.key: f for f in assess(open_pbip(SINGLE))}
    assert alone["Sales[Order Number]"].verdict is Verdict.REMOVE

    workspace = discover_workspace(WS)
    together = {f.key: f for f in workspace.findings_for("Contoso")}
    assert together["Sales[Order Number]"].verdict is not Verdict.REMOVE
    assert together["Sales[Order Number]"].report_bindings == 1


def test_reports_using_names_the_visual(workspace):
    """A count is not enough to hand to someone else; a list is."""
    assert workspace.reports_using("Contoso", "Sales[Order Number]") == [
        "Exec.Report / Board / table (field-well)"
    ]


def test_usage_is_unioned_across_the_reports(workspace):
    found = {f.key: f for f in workspace.findings_for("Contoso")}
    # Product[Product Name] comes from Contoso.Report, Order Number from Exec.Report.
    assert found["Product[Product Name]"].report_bindings >= 1
    assert found["Sales[Order Number]"].report_bindings >= 1


# -- the asymmetry rule, doing real work--------------------------------------------------------


def test_an_unmatched_report_degrades_every_model(workspace):
    """The stray report could belong to any model here, so it degrades all of them."""
    for name in workspace.models:
        assert workspace.scope_for(name).reports_unmatched == 1
        assert not workspace.scope_for(name).trustworthy


def test_the_degrade_turns_remove_into_unknown(workspace):
    """Regional[Region Code] is genuinely unreferenced, and still must not say REMOVE."""
    found = {f.key: f for f in workspace.findings_for("Regional")}
    assert found["Region[Region Code]"].verdict is Verdict.UNKNOWN
    assert "could not be matched" in found["Region[Region Code]"].reason


def test_removing_the_stray_report_restores_remove(tmp_path):
    """Proves the degrade is caused by the unmatched report and nothing else."""
    import shutil

    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    shutil.rmtree(copy / "Stray.Report")

    workspace = discover_workspace(copy)
    assert workspace.unmatched == []
    assert workspace.scope_for("Regional").trustworthy
    found = {f.key: f for f in workspace.findings_for("Regional")}
    assert found["Region[Region Code]"].verdict is Verdict.REMOVE


def test_the_workspace_warns_about_unmatched_reports(workspace):
    joined = " ".join(workspace.warnings)
    assert "could not be matched" in joined
    assert "downgraded to UNKNOWN" in joined
    assert "Stray.Report" in joined


# -- awkward inputs-----------------------------------------------------------------------------


def test_a_report_with_no_pbir_is_unmatched(tmp_path):
    (tmp_path / "Bare.Report" / "definition").mkdir(parents=True)
    workspace = discover_workspace(tmp_path)
    assert workspace.unmatched[0].reason.startswith("no definition.pbir")


def test_a_broken_pbir_is_unmatched_not_fatal(tmp_path):
    report = tmp_path / "Broken.Report"
    report.mkdir()
    (report / "definition.pbir").write_text("{ not json", encoding="utf-8")
    workspace = discover_workspace(tmp_path)
    assert "could not be read" in workspace.unmatched[0].reason


def test_a_pbir_naming_a_path_outside_the_scan_is_unmatched(tmp_path):
    report = tmp_path / "Far.Report"
    report.mkdir()
    (report / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../../Elsewhere.SemanticModel"}}}),
        encoding="utf-8",
    )
    workspace = discover_workspace(tmp_path)
    assert "not a model in this scan" in workspace.unmatched[0].reason


def test_duplicate_model_names_are_reported(tmp_path):
    """Two models with the same name make byConnection matching ambiguous."""
    import shutil

    for parent in ("a", "b"):
        target = tmp_path / parent / "Sales.SemanticModel"
        shutil.copytree(f"{SINGLE}/Contoso.SemanticModel", target)
    workspace = discover_workspace(tmp_path)
    assert any("both called 'Sales'" in w for w in workspace.warnings)


def test_a_model_that_will_not_load_does_not_stop_the_scan(tmp_path):
    import shutil

    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    shutil.rmtree(copy / "Orphan.SemanticModel" / "definition")
    workspace = discover_workspace(copy)
    assert "Orphan" not in workspace.models
    assert any("Orphan" in w and "could not load" in w for w in workspace.warnings)
    assert "Contoso" in workspace.models  # the rest still loaded


# -- cross-model coupling-----------------------------------------------------------------------


def test_a_model_reading_another_model_is_recorded(tmp_path):
    import shutil

    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    table = copy / "Regional.SemanticModel" / "definition" / "tables" / "Region.tmdl"
    text = table.read_text(encoding="utf-8").replace(
        "source = let x = 1 in x",
        'source = let S = AnalysisServices.Database("powerbi://api", "Contoso") in S',
    )
    table.write_text(text, encoding="utf-8")

    workspace = discover_workspace(copy)
    assert ("Regional", "Contoso") in workspace.cross_model


def test_coupling_to_an_unknown_target_is_recorded_without_guessing(tmp_path):
    import shutil

    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    table = copy / "Regional.SemanticModel" / "definition" / "tables" / "Region.tmdl"
    table.write_text(
        table.read_text(encoding="utf-8").replace(
            "source = let x = 1 in x",
            'source = let S = AnalysisServices.Database("srv", "Somewhere Else") in S',
        ),
        encoding="utf-8",
    )
    workspace = discover_workspace(copy)
    assert ("Regional", "") in workspace.cross_model
    assert any("target is not a model in this scan" in w for w in workspace.warnings)


def test_a_model_only_consumed_by_another_model_is_not_an_orphan(tmp_path):
    """Orphan means nothing uses it. Another model reading it counts as use."""
    import shutil

    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    table = copy / "Regional.SemanticModel" / "definition" / "tables" / "Region.tmdl"
    table.write_text(
        table.read_text(encoding="utf-8").replace(
            "source = let x = 1 in x",
            'source = let S = AnalysisServices.Database("srv", "Orphan") in S',
        ),
        encoding="utf-8",
    )
    workspace = discover_workspace(copy)
    assert "Orphan" not in workspace.orphan_models()
