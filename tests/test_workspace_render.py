"""The workspace index and its per-model drill-down."""

from __future__ import annotations

import re
import shutil

import pytest

from dax_quax.cli import main
from dax_quax.render.workspace import build_workspace_context, render_workspace, slugify
from dax_quax.sources.workspace import discover_workspace

WS = "tests/fixtures/synthetic_workspace"


@pytest.fixture
def workspace():
    return discover_workspace(WS)


@pytest.fixture
def complete(tmp_path):
    """The same workspace with the unmatched report removed, so verdicts are trustworthy."""
    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    shutil.rmtree(copy / "Stray.Report")
    return discover_workspace(copy)


@pytest.fixture
def rendered(workspace, tmp_path):
    index = render_workspace(workspace, tmp_path / "out")
    return index.read_text(encoding="utf-8")


# -- files ---------------------------------------------------------------------------------


def test_one_page_per_model_plus_an_index(workspace, tmp_path):
    out = tmp_path / "out"
    index = render_workspace(workspace, out)
    assert index.name == "index.html"
    assert {path.name for path in out.glob("*.html")} == {
        "index.html",
        "contoso.html",
        "orphan.html",
        "regional.html",
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Contoso", "contoso"),
        ("Sales & Margin", "sales-margin"),
        ("Model (v2)", "model-v2"),
        ("  ", "model"),
    ],
)
def test_slugify(name, expected):
    """Model names allow spaces and punctuation; filenames do not."""
    assert slugify(name) == expected


def test_the_index_needs_no_network(rendered):
    assert re.findall(r'(?:src|href)\s*=\s*["\'](https?:|//)', rendered) == []


# -- coverage, prominently ---------------------------------------------------------------------


def test_the_index_leads_with_its_coverage(rendered):
    assert "3 model(s), 4 report(s), 3 matched, 1 unmatched" in rendered


def test_removable_is_withheld_when_the_scan_is_incomplete(rendered):
    """The count is not shown at all rather than shown as a reassuring zero."""
    assert "withheld" in rendered
    assert "no object anywhere in this scan is reported as removable" in rendered


def test_removable_is_shown_once_every_report_is_matched(complete, tmp_path):
    html = render_workspace(complete, tmp_path / "ok").read_text(encoding="utf-8")
    assert "withheld" not in html
    assert "Every report in this directory was matched" in html


def test_the_link_table_lists_every_report_and_why_one_failed(rendered):
    for name in ("Contoso.Report", "Exec.Report", "Regional.Report", "Stray.Report"):
        assert name in rendered
    assert "byPath ../Contoso.SemanticModel" in rendered
    assert "byConnection Regional" in rendered
    assert "Warehouse Finance" in rendered


def test_orphan_models_are_called_out(rendered):
    assert "ORPHAN MODELS" in rendered
    assert "no report in this scan references it" in rendered


def test_the_index_states_its_own_limits(rendered):
    assert "WHAT THIS SCAN COULD NOT SEE" in rendered
    assert "source, not deployment" in rendered
    assert "Analyze in Excel" in rendered
    assert "byConnection matching is by dataset" in rendered


# -- the drill-down inherits the workspace's scope -------------------------------------------------


def test_a_model_page_carries_the_workspace_scope(workspace, tmp_path):
    """Otherwise the per-model page would claim a confidence the index has withheld."""
    out = tmp_path / "out"
    render_workspace(workspace, out)
    html = (out / "regional.html").read_text(encoding="utf-8")
    assert "1 report scanned, 1 unmatched" in html
    assert "No verdict below claims an object is removable" in html


def test_a_model_page_links_back_to_the_index(workspace, tmp_path):
    out = tmp_path / "out"
    render_workspace(workspace, out)
    assert 'href="index.html"' in (out / "contoso.html").read_text(encoding="utf-8")


def test_the_degrade_reaches_the_model_page(workspace, tmp_path):
    out = tmp_path / "out"
    render_workspace(workspace, out)
    html = (out / "regional.html").read_text(encoding="utf-8")
    assert '"verdict": "UNKNOWN"' in html
    assert '"verdict": "REMOVE"' not in html


def test_a_complete_scan_reaches_remove_on_the_model_page(complete, tmp_path):
    out = tmp_path / "ok"
    render_workspace(complete, out)
    assert '"verdict": "REMOVE"' in (out / "regional.html").read_text(encoding="utf-8")


# -- per-model cards----------------------------------------------------------------------------


def test_a_card_previews_its_actionable_findings(complete, tmp_path):
    html = render_workspace(complete, tmp_path / "ok").read_text(encoding="utf-8")
    assert "Region[Region Code]" in html


def test_a_card_names_who_uses_a_finding(complete):
    """The point of the workspace: not a count, but which report breaks."""
    context = build_workspace_context(complete)
    contoso = next(m for m in context["models"] if m["name"] == "Contoso")
    assert contoso["scope"] == "2 reports scanned"
    used = {u for finding in contoso["findings"] for u in finding["used_by"]}
    assert all("Report" in entry for entry in used)


def test_cards_are_ordered_and_linked(rendered):
    for href in ("contoso.html", "orphan.html", "regional.html"):
        assert f'href="{href}"' in rendered


# -- CLI----------------------------------------------------------------------------------------


def test_cli_report_workspace(tmp_path, capsys):
    code = main(["report", "--workspace", WS, "--out", str(tmp_path / "site")])
    assert code == 0
    out = capsys.readouterr().out
    assert "index.html" in out
    assert "3 model(s)" in out
    assert (tmp_path / "site" / "index.html").is_file()


def test_cli_report_workspace_tolerates_a_filename_for_out(tmp_path, capsys):
    """`--out foo.html` on a workspace means a directory; do not write one file called that."""
    main(["report", "--workspace", WS, "--out", str(tmp_path / "site.html")])
    assert (tmp_path / "site" / "index.html").is_file()
