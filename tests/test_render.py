"""Report rendering and CLI tests."""

from __future__ import annotations

import json
import re

import pytest

from dax_quax.cli import main
from dax_quax.render.report import build_context, format_bytes, render_report

FIXTURE_ROWSETS = "tests/fixtures/synthetic/rowsets.json"
FIXTURE_REPORT = "tests/fixtures/synthetic_pbip"


# -- byte formatting ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, "—"),
        (0, "0 B"),
        (512, "512 B"),
        (2048, "2.0 KB"),
        (7025, "6.9 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.00 GB"),
    ],
)
def test_format_bytes(size, expected):
    assert format_bytes(size) == expected


def test_unmeasured_is_a_dash_not_a_zero():
    """The whole no-zeros-for-unmeasured rule, at the last place it could be broken."""
    assert format_bytes(None) == "—"
    assert format_bytes(0) == "0 B"


# -- context ---------------------------------------------------------------------------------


def test_context_has_no_model_objects(model_with_report):
    """The template gets formatted primitives only."""
    context = build_context(model_with_report)
    assert json.dumps(context["rows"])  # serialisable, so nothing exotic leaked through
    assert context["summary"]["removable_objects"] == 1


def test_context_bar_is_relative_to_the_largest(model_with_report):
    rows = build_context(model_with_report)["rows"]
    assert rows[0]["bar_pct"] == 100.0
    assert all(r["bar_pct"] <= 100.0 for r in rows)


def test_context_blind_spots_are_carried_through(model_with_report):
    joined = " ".join(build_context(model_with_report)["blind_spots"])
    assert "invisible to a local scan" in joined


def test_unresolved_dax_becomes_a_warning(model_with_report):
    model_with_report.measures["[Total Sales]"].expression = "SUM ( Sales[Ghost] )"
    warnings = build_context(model_with_report)["warnings"]
    assert any("could not be resolved to a model object" in w for w in warnings)


def test_unparsed_report_references_become_a_warning(model_with_report):
    warnings = build_context(model_with_report)["warnings"]
    assert any("could not be resolved to a table" in w for w in warnings)


# -- the rendered file --------------------------------------------------------------------------


@pytest.fixture
def rendered(model_with_report, tmp_path):
    path = render_report(model_with_report, tmp_path / "report.html")
    return path.read_text(encoding="utf-8")


def test_report_is_written(model_with_report, tmp_path):
    path = render_report(model_with_report, tmp_path / "out" / "report.html")
    assert path.is_file()
    assert path.stat().st_size > 5000


def test_report_needs_no_network(rendered):
    """Self-contained means self-contained: no CDN, no webfonts, no external anything.

    A report that needs the internet to render is a bookmark, not a report.
    """
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?:|//)', rendered)
    assert external == []
    assert "fonts.googleapis" not in rendered
    assert "cdn" not in rendered.lower().replace("cdn-", "")


def test_report_contains_the_findings(rendered):
    assert "Order Number" in rendered
    assert "REMOVE" in rendered
    assert "Remove it from the Power Query load." in rendered


def test_report_states_what_it_could_not_see(rendered):
    assert "WHAT THIS SCAN COULD NOT SEE" in rendered
    assert "invisible to a local scan" in rendered


def test_report_embeds_data_as_json(rendered):
    payload = re.search(
        r'<script type="application/json" id="data">(.*?)</script>', rendered, re.S
    )
    assert payload
    rows = json.loads(payload.group(1))
    assert any(r["key"] == "Sales[Order Number]" and r["verdict"] == "REMOVE" for r in rows)


def test_report_escapes_the_model_name(model_with_report, tmp_path):
    model_with_report.name = '<script>alert("x")</script>'
    html = render_report(model_with_report, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_report_renders_without_metrics(metadata_only_model, tmp_path):
    """A PBIP with no cache.abf still produces a usable report, with sizes as dashes."""
    html = render_report(metadata_only_model, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "MEASURED SIZE" in html
    assert "UNKNOWN" in html


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_scan_replays_a_recorded_scan(capsys):
    assert main(["scan", "--rowsets", FIXTURE_ROWSETS, "--report-folder", FIXTURE_REPORT]) == 0
    out = capsys.readouterr().out
    assert "REMOVE" in out
    assert "Sales[Order Number]" in out


def test_cli_scan_without_a_report_says_why_everything_is_unknown(capsys):
    assert main(["scan", "--rowsets", FIXTURE_ROWSETS]) == 0
    out = capsys.readouterr().out
    assert "scope: no report scanned" in out
    assert "UNKNOWN rather than REMOVE" in out
    assert "--report-folder" in out


def test_cli_scan_json(capsys):
    assert main(["scan", "--rowsets", FIXTURE_ROWSETS, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["objects"] > 0
    assert payload["findings"]


def test_cli_report_writes_a_file(tmp_path, capsys):
    out = tmp_path / "r.html"
    code = main(
        [
            "report",
            "--rowsets", FIXTURE_ROWSETS,
            "--report-folder", FIXTURE_REPORT,
            "--out", str(out),
        ]
    )
    assert code == 0
    assert out.is_file()
    assert "wrote" in capsys.readouterr().out


def test_cli_threshold_changes_the_verdicts(capsys):
    main(["scan", "--rowsets", FIXTURE_ROWSETS, "--report-folder", FIXTURE_REPORT,
          "--large-mb", "0.0004"])
    aggressive = capsys.readouterr().out
    main(["scan", "--rowsets", FIXTURE_ROWSETS, "--report-folder", FIXTURE_REPORT,
          "--large-mb", "1000"])
    relaxed = capsys.readouterr().out
    assert aggressive != relaxed


def test_cli_serve_parses_without_starting_a_server():
    """`main(["serve"])` would block on uvicorn forever, so only the parser is exercised.

    This test hung the entire suite when serve stopped being a stub.
    """
    from dax_quax.cli import build_parser

    args = build_parser().parse_args(
        ["serve", "--pbip", FIXTURE_REPORT, "--http-port", "9999", "--no-browser"]
    )
    assert args.http_port == 9999
    assert args.no_browser
    assert args.host == "127.0.0.1"  # localhost only: this exposes model metadata


def test_embedded_json_cannot_close_its_script_element(model_with_report, tmp_path):
    """An object named "</script>" must not be able to end the data block.

    json.dumps does not escape '<', so without the escape pass everything after such a
    name would be parsed as HTML rather than data.
    """
    measure = model_with_report.measures["[Total Sales]"]
    measure.name = '</script><img src=x onerror=alert(1)>'
    model_with_report.measures[measure.key] = measure
    html = render_report(model_with_report, tmp_path / "r.html").read_text(encoding="utf-8")
    payload = re.search(
        r'<script type="application/json" id="data">(.*?)</script>', html, re.S
    )
    assert payload
    rows = json.loads(payload.group(1))          # still valid JSON
    assert any("</script>" in r["name"] for r in rows)   # and the name survived intact
    assert "onerror=alert(1)>" not in html.replace(payload.group(1), "")


def test_module_entry_point_exists():
    """`python -m dax_quax.cli` must work, not only the installed console script."""
    import importlib.util

    assert importlib.util.find_spec("dax_quax.cli.__main__") is not None


def test_table_scrolls_rather_than_clipping_on_narrow_screens(rendered):
    """Below roughly 1200px the right-hand columns - VERDICT among them - were cut off."""
    assert 'class="tablewrap"' in rendered
    assert "overflow-x: auto" in rendered


def test_report_headlines_its_scope(rendered):
    assert "Scope:" in rendered
    assert "1 report scanned" in rendered


def test_report_says_nothing_is_removable_when_the_scope_is_weak(model_with_report, tmp_path):
    """An unmatched report must be visible at the top, not inferred from the table."""
    from dax_quax.analysis.usage import ScanScope, assess

    lineage = model_with_report.lineage()
    scope = ScanScope(model_source="pbip", reports_scanned=3, reports_unmatched=1)
    findings = assess(model_with_report, lineage=lineage, scope=scope)
    assert not any(str(f.verdict) == "REMOVE" for f in findings)


# -- CLI gaps found in review -----------------------------------------------------------


FIXTURE_WS = "tests/fixtures/synthetic_workspace"


def test_cli_scan_workspace_json(capsys):
    """A workspace scan was printing only human text, so a pipeline could not consume it."""
    assert main(["scan", "--workspace", FIXTURE_WS, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"] == "3 model(s), 4 report(s), 3 matched, 1 unmatched"
    assert payload["trustworthy"] is False
    assert payload["unmatched"] == ["Stray.Report"]
    assert payload["orphan_models"] == ["Orphan"]
    assert "Contoso" in payload["models"]
    assert payload["models"]["Contoso"]["findings"]


def test_cli_serve_routes_a_workspace_to_the_workspace_server(monkeypatch):
    """`main(["serve"])` would block on uvicorn forever, so only the routing is exercised."""
    import dax_quax.render.serve as serve_module

    seen = {}
    monkeypatch.setattr(
        serve_module, "serve_switchboard", lambda initial, **kw: seen.update(kw, initial=initial)
    )
    assert main(["serve", "--workspace", FIXTURE_WS, "--no-browser"]) == 0
    shape, _loader, description = seen["initial"]
    assert shape == "workspace"
    assert description.startswith("workspace ")
    assert seen["open_browser"] is False


def test_model_exposes_findings_without_reaching_into_analysis(model_with_report):
    """CONVENTIONS section 9 advertised this; it did not exist."""
    findings = model_with_report.findings()
    assert findings
    assert {"table", "object", "verdict"} <= set(model_with_report.findings_frame().columns)


def test_importing_the_module_entry_point_does_not_run_the_cli():
    """Without the __name__ guard, anything that walks the package runs the CLI.

    pkgutil.walk_packages does, which is how the CI import check found it.
    """
    import importlib
    import sys

    sys.modules.pop("dax_quax.cli.__main__", None)
    importlib.import_module("dax_quax.cli.__main__")  # must not raise SystemExit
