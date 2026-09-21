"""Where an object is defined, and the links that go there.

The discipline is the same as everywhere else in this tool: a source that cannot know says
nothing rather than guessing. A path that points at no file is worse than no path, because
it is the one thing a reader will not double-check.
"""

from __future__ import annotations

import pathlib

import pytest

from dax_quax.model import SourceRef
from dax_quax.render.editor import EDITORS, editor_uri
from dax_quax.sources.pbip import open_pbip

PBIP = "tests/fixtures/synthetic_pbip"


@pytest.fixture
def model():
    return open_pbip(PBIP)


# -- what a TMDL source knows ----------------------------------------------------------


def test_a_tmdl_source_knows_where_everything_is(model):
    assert model.has("locations")
    for key in ("Sales", "Sales[Line Amount]", "[Total Sales]"):
        assert model.where(key) is not None, key


def test_the_line_is_the_object_not_the_file(model):
    """A file-level link on a 400-line table file sends you hunting."""
    table = model.where("Sales").line
    measure = model.where("[Total Sales]").line
    assert table == 1
    assert measure > table


def test_a_measure_and_a_column_in_one_file_get_different_lines(model):
    assert model.where("Sales[Quantity]").line != model.where("Sales[Line Amount]").line


def test_the_path_is_shown_relative_to_the_project(model):
    where = model.where("[Total Sales]").describe(model.root)
    assert where.startswith("Contoso.SemanticModel/definition/tables/Sales.tmdl:")
    assert ":" in where


def test_the_stored_path_is_absolute(model):
    """Relative is for reading; an editor needs the real one."""
    assert model.where("[Total Sales]").path.is_absolute()


def test_a_path_outside_the_project_is_not_mangled_into_one(model):
    outside = SourceRef(path=pathlib.Path("/elsewhere/other.tmdl"), line=3)
    assert outside.describe(model.root) == "/elsewhere/other.tmdl:3"


def test_a_ref_with_no_line_says_only_the_file():
    assert SourceRef(path=pathlib.Path("a/b.json")).describe() == "a/b.json"


# -- what other sources do not know ------------------------------------------------------


def test_a_replayed_scan_claims_nothing(synthetic_model):
    """A DMV rowset has no files. Absent, not fabricated."""
    assert not synthetic_model.has("locations")
    assert synthetic_model.locations == {}
    assert synthetic_model.where("Sales[Order Number]") is None


def test_the_capability_is_claimed_only_when_something_was_recorded(synthetic_model):
    assert "locations" not in synthetic_model.capabilities


# -- the report side -------------------------------------------------------------------


def test_a_binding_carries_the_visual_json_it_came_from(model):
    binding = model.reports[0].for_key("[Total Sales]")[0]
    assert binding.file.name == "visual.json"
    assert binding.file.is_file()


def test_a_visual_carries_its_own_file(model):
    visual = model.reports[0].visuals[0]
    assert visual.file.name == "visual.json"


def test_a_page_carries_its_page_json(model):
    report = model.reports[0]
    assert report.page_files["Overview"].name == "page.json"


def test_a_report_finding_points_at_the_file_to_open(model):
    from dax_quax.analysis.report_findings import report_findings

    del model.columns["Product[Product Name]"]
    broken = [f for f in report_findings(model) if f.kind == "broken-binding"]
    assert broken[0].file.name == "visual.json"


def test_an_empty_page_points_at_the_page_not_a_visual(model):
    from dax_quax.analysis.report_findings import report_findings

    report = model.reports[0]
    report.pages = (*report.pages, "Scratch")
    empty = [f for f in report_findings(model) if f.kind == "empty-page"]
    # The fixture has no Scratch page on disk, so there is no file. Say nothing.
    assert empty[0].file is None


# -- editor links ------------------------------------------------------------------------


def test_the_default_link_opens_an_editor_at_the_line(model):
    uri = editor_uri(model.where("[Total Sales]"))
    assert uri.startswith("vscode://file/")
    assert uri.endswith(f":{model.where('[Total Sales]').line}")


def test_a_windows_path_keeps_its_drive_and_uses_forward_slashes(model):
    uri = editor_uri(model.where("[Total Sales]"))
    assert "\\" not in uri


def test_none_links_nothing(model):
    """A URI nothing on the machine handles fails silently; `none` is the honest opt-out."""
    assert editor_uri(model.where("[Total Sales]"), "none") is None


def test_an_unknown_editor_links_nothing_rather_than_guessing(model):
    assert editor_uri(model.where("[Total Sales]"), "emacs-via-carrier-pigeon") is None


def test_no_location_means_no_link():
    assert editor_uri(None) is None


def test_a_ref_with_no_line_still_opens_the_file():
    """Line 0 would send an editor to the top and look like a miss."""
    uri = editor_uri(SourceRef(path=pathlib.Path("/a/b.json")))
    assert uri.endswith(":1")


def test_every_editor_template_takes_both_fields():
    ref = SourceRef(path=pathlib.Path("/a/b.tmdl"), line=7)
    for name in EDITORS:
        editor_uri(ref, name)  # must not raise


# -- surfacing -------------------------------------------------------------------------


def test_the_report_row_carries_the_location(model):
    from dax_quax.render.report import build_context

    row = next(r for r in build_context(model)["rows"] if r["key"] == "[Total Sales]")
    assert row["defined_in"].endswith(".tmdl:35")
    assert row["defined_uri"].startswith("vscode://file/")


def test_the_report_row_says_nothing_when_the_source_cannot(synthetic_model):
    from dax_quax.render.report import build_context

    context = build_context(synthetic_model)
    assert context["has_locations"] is False
    assert all(row["defined_in"] is None for row in context["rows"])


def test_the_editor_choice_reaches_the_rendered_page(model, tmp_path):
    from dax_quax.render.report import render_report

    out = tmp_path / "r.html"
    render_report(model, out, editor="none")
    html = out.read_text(encoding="utf-8")
    assert "vscode://" not in html
    # The path is still readable; only the link is gone.
    assert "Sales.tmdl" in html


def test_the_rendered_page_links_to_the_file(model, tmp_path):
    from dax_quax.render.report import render_report

    out = tmp_path / "r.html"
    render_report(model, out)
    html = out.read_text(encoding="utf-8")
    assert "DEFINED IN" in html
    assert "vscode://file/" in html


def test_the_cli_prints_the_file_under_a_finding(capsys):
    from dax_quax.cli import main

    assert main(["scan", "--pbip", PBIP]) == 0
    out = capsys.readouterr().out
    assert "Sales.tmdl:" in out
    # A terminal makes path:line clickable by itself; a vscode:// URI would be noise.
    assert "vscode://" not in out


def test_the_cli_editor_choice_is_offered_where_html_is_written():
    from dax_quax.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["report", "--pbip", PBIP, "--editor", "none"]).editor == "none"
    assert parser.parse_args(["serve", "--pbip", PBIP]).editor == "vscode"


def test_mcp_hands_an_agent_the_file(model):
    from dax_quax.mcp.tools import Session, call

    session = Session(loader=lambda: model, description=PBIP)
    payload = call(session, "explain_object", {"key": "Total Sales"})["result"]
    assert payload["defined_in"].endswith(".tmdl:35")


def test_mcp_says_when_a_source_has_no_files(synthetic_model):
    from dax_quax.mcp.tools import Session, call

    session = Session(loader=lambda: synthetic_model, description="replay")
    payload = call(session, "describe_model", {})["result"]
    assert "no files" in payload["source_files"]


def test_the_page_shows_a_relative_path_and_links_the_absolute_one(model):
    """Absolute is what an editor needs; relative is what a reader wants to read."""
    from dax_quax.render.report import build_context

    row = build_context(model)["report_findings"][0]
    assert row["file"].startswith("Contoso.Report/")
    assert row["uri"].startswith("vscode://file/C:") or row["uri"].startswith("vscode://file//")


def test_scan_prints_the_model_warnings(capsys):
    """They were in --json and nowhere else, so the terminal read clean on a broken scan."""
    from dax_quax.cli import main

    assert main(["scan", "--pbip", PBIP]) == 0
    assert "! no .pbi/cache.abf" in capsys.readouterr().out
