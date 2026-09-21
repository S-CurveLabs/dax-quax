"""Findings about the report rather than the model.

The most valuable one inverts everything else here: an object a visual binds but the model
does not have is not a deletion candidate, it is a visual that is broken in production now.
"""

from __future__ import annotations

import argparse
import json
import shutil

import pytest

from dax_quax.analysis.report_findings import ReportFinding, report_findings
from dax_quax.report import Binding, ReportBindings, Visual, attach_report, load_report
from dax_quax.sources.pbip import open_pbip

PBIP = "tests/fixtures/synthetic_pbip"


def kinds(findings: list[ReportFinding]) -> set[str]:
    return {finding.kind for finding in findings}


def of_kind(findings: list[ReportFinding], kind: str) -> list[ReportFinding]:
    return [finding for finding in findings if finding.kind == kind]


@pytest.fixture
def model():
    return open_pbip(PBIP)


# -- the fixture is healthy ------------------------------------------------------------


def test_a_sound_report_has_no_breakage(model):
    findings = report_findings(model)
    assert "broken-binding" not in kinds(findings)
    assert "report-mismatch" not in kinds(findings)
    assert "empty-visual" not in kinds(findings)


def test_a_model_with_no_report_yields_nothing(synthetic_model):
    assert report_findings(synthetic_model) == []


# -- broken bindings -------------------------------------------------------------------------


def test_a_binding_the_model_cannot_satisfy_is_broken(model):
    """Someone deleted the column; the visual still asks for it."""
    del model.columns["Product[Product Name]"]
    broken = of_kind(report_findings(model), "broken-binding")
    assert broken
    assert "Product[Product Name]" in broken[0].objects
    assert "the model does not have" in broken[0].detail
    assert broken[0].page == "Overview"


def test_a_broken_binding_names_where_it_lives(model):
    del model.columns["Product[Product Name]"]
    finding = of_kind(report_findings(model), "broken-binding")[0]
    assert finding.where.startswith("Contoso.Report / Overview")


def test_mostly_broken_means_the_wrong_model_not_a_broken_report(model):
    """A report attached to the wrong model would otherwise produce a wall of breakage."""
    model.columns.clear()
    model.measures.clear()
    findings = report_findings(model)
    assert of_kind(findings, "broken-binding") == []
    mismatch = of_kind(findings, "report-mismatch")
    assert len(mismatch) == 1
    assert "belongs to a different model" in mismatch[0].detail


def test_an_unparsed_binding_is_not_counted_as_broken(model):
    """The fixture has one reference the parser could not read. Unknown is not broken."""
    assert model.reports[0].unparsed
    assert of_kind(report_findings(model), "broken-binding") == []


# -- empty things ------------------------------------------------------------------------------


def test_a_data_visual_with_no_fields_is_a_finding(model):
    model.reports[0].visuals.append(Visual(page="Overview", name="v9", visual_type="lineChart"))
    findings = of_kind(report_findings(model), "empty-visual")
    assert len(findings) == 1
    assert "empty frame" in findings[0].detail


def test_a_text_box_with_no_fields_is_not(model):
    """A text box carries no data by design; a line chart with none is broken."""
    model.reports[0].visuals.append(Visual(page="Overview", name="t1", visual_type="textbox"))
    model.reports[0].visuals.append(Visual(page="Overview", name="i1", visual_type="image"))
    assert of_kind(report_findings(model), "empty-visual") == []


def test_a_page_with_no_visuals_is_a_finding(model):
    report = model.reports[0]
    report.pages = (*report.pages, "Scratch")
    findings = of_kind(report_findings(model), "empty-page")
    assert [f.page for f in findings] == ["Scratch"]


# -- duplicates ----------------------------------------------------------------------------------


def test_the_same_field_twice_in_one_visual(model):
    report = model.reports[0]
    report.bindings.append(
        Binding(entity="Product", property="Product Name", kind="column",
                location="field-well", page="Overview", visual="v1", report=report.name)
    )
    findings = of_kind(report_findings(model), "duplicate-binding")
    assert len(findings) == 1
    assert "bound 2 times" in findings[0].detail


def test_a_field_in_both_a_well_and_a_filter_is_not_a_duplicate(model):
    """Legitimate: the same column on an axis and in a filter is normal."""
    report = model.reports[0]
    report.bindings.append(
        Binding(entity="Product", property="Product Name", kind="column",
                location="filter", page="Overview", visual="v1", report=report.name)
    )
    assert of_kind(report_findings(model), "duplicate-binding") == []


# -- single use ------------------------------------------------------------------------------------


def test_an_object_used_once_is_reported(model):
    findings = of_kind(report_findings(model), "single-use")
    assert any("Product[Product Name]" in f.objects for f in findings)
    assert "used once, by" in findings[0].detail


def test_single_use_carries_the_cost_when_it_is_known(model_with_report):
    """Used once is not a problem. Used once and costing 7 KB is a question."""
    findings = of_kind(report_findings(model_with_report), "single-use")
    with_size = [f for f in findings if "costs" in f.detail]
    assert with_size, "a measured column used once should say what it costs"


def test_an_object_used_by_two_visuals_is_not_single_use(model):
    report = model.reports[0]
    report.bindings.append(
        Binding(entity="Product", property="Product Name", kind="column",
                location="field-well", page="Overview", visual="v2",
                visual_type="card", report=report.name)
    )
    findings = of_kind(report_findings(model), "single-use")
    assert not any("Product[Product Name]" in f.objects for f in findings)


# -- across several reports -------------------------------------------------------------------- ---


def test_usage_is_counted_across_every_attached_report(model, tmp_path):
    """Single-use must mean once in the whole report layer, not once per report."""
    second = ReportBindings(name="Exec.Report")
    second.bindings.append(
        Binding(entity="Product", property="Product Name", kind="column",
                location="field-well", page="Board", visual="x", report="Exec.Report")
    )
    attach_report(model, second)
    findings = of_kind(report_findings(model), "single-use")
    assert not any("Product[Product Name]" in f.objects for f in findings)


def test_findings_name_their_report(model, tmp_path):
    copied = tmp_path / "Second.Report"
    shutil.copytree(f"{PBIP}/Contoso.Report", copied)
    (copied / "definition.pbir").write_text(
        json.dumps({"version": "1.0", "datasetReference": {"byPath": {"path": ".."}}}),
        encoding="utf-8",
    )
    attach_report(model, load_report(copied))
    del model.columns["Product[Product Name]"]
    reports = {f.report for f in of_kind(report_findings(model), "broken-binding")}
    assert reports == {"Contoso.Report", "Second.Report"}


def test_describe_is_one_readable_line(model):
    del model.columns["Product[Product Name]"]
    line = of_kind(report_findings(model), "broken-binding")[0].describe()
    assert line.startswith("broken-binding")
    assert "Contoso.Report" in line


# -- surfacing: a finding nobody sees is not a feature ---------------------------------


def test_the_cli_prints_the_report_layer(capsys):
    from dax_quax.cli import main

    assert main(["scan", "--pbip", PBIP]) == 0
    out = capsys.readouterr().out
    assert "report layer:" in out
    assert "single-use" in out


def test_the_cli_says_nothing_when_no_report_was_scanned(capsys):
    from dax_quax.cli import main

    assert main(["scan", "--rowsets", "tests/fixtures/synthetic/rowsets.json"]) == 0
    assert "report layer:" not in capsys.readouterr().out


def test_the_cli_counts_breakage_separately(model, capsys):
    """A page of findings is noise unless it says how many are wrong right now."""
    import dax_quax.cli as cli

    del model.columns["Product[Product Name]"]
    cli._print_report_findings(model, argparse.Namespace(top=20))
    out = capsys.readouterr().out
    assert "already broken" in out
    # Breakage is read first, whatever order the findings were produced in.
    assert out.index("broken-binding") < out.index("single-use")


def test_the_html_report_carries_the_findings(model):
    from dax_quax.render.report import build_context

    del model.columns["Product[Product Name]"]
    rows = build_context(model)["report_findings"]
    assert rows
    assert rows[0]["broken"] is True
    assert rows[0]["subject"] == "Product[Product Name]"
    assert rows[0]["kind"] == "broken-binding"
    assert any(row["broken"] is False for row in rows)


def test_the_rendered_html_shows_them(model, tmp_path):
    from dax_quax.render.report import render_report

    del model.columns["Product[Product Name]"]
    out = tmp_path / "r.html"
    render_report(model, out)
    html = out.read_text(encoding="utf-8")
    assert "THE REPORT LAYER" in html
    assert "broken-binding" in html
    assert "the model does not have" in html
