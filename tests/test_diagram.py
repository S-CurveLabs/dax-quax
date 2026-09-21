"""Diagrams: the rolled-up view, and the route highlighting over it."""

from __future__ import annotations

import json
import re
import xml.dom.minidom

import pytest

from dax_quax.analysis.diagram import (
    DiagramEdge,
    layer,
    model_diagram,
    workspace_diagram,
)
from dax_quax.render.diagram import WRAP_AT, render_diagram
from dax_quax.render.report import render_report
from dax_quax.sources.workspace import discover_workspace

WS = "tests/fixtures/synthetic_workspace"


def svg_of(block: str) -> str:
    return re.search(r"<svg.*?</svg>", block, re.S).group(0)


@pytest.fixture
def model(model_with_report):
    return model_diagram(model_with_report)


@pytest.fixture
def workspace():
    return workspace_diagram(discover_workspace(WS))


# -- layering ------------------------------------------------------------------------------


def test_longest_path_layering():
    edges = [DiagramEdge("a", "b", "expression"), DiagramEdge("b", "c", "expression")]
    tiers, cycled = layer(["a", "b", "c"], edges)
    assert (tiers["a"], tiers["b"], tiers["c"]) == (0, 1, 2)
    assert cycled == []


def test_a_node_sits_right_of_its_deepest_predecessor():
    """Not its first: a short path must not pull a node left of a long one."""
    edges = [
        DiagramEdge("a", "d", "expression"),
        DiagramEdge("a", "b", "expression"),
        DiagramEdge("b", "c", "expression"),
        DiagramEdge("c", "d", "expression"),
    ]
    tiers, _ = layer(["a", "b", "c", "d"], edges)
    assert tiers["d"] == 3


def test_a_cycle_is_reported_rather_than_hidden():
    edges = [DiagramEdge("a", "b", "relationship"), DiagramEdge("b", "a", "relationship")]
    _, cycled = layer(["a", "b"], edges)
    assert set(cycled) == {"a", "b"}


def test_a_cycle_is_noted_on_the_diagram(model_with_report):
    model_with_report.relationships.append(
        type(model_with_report.relationships[0])(
            from_table="Product", from_column="Product Key",
            to_table="Sales", to_column="Product Key",
        )
    )
    diagram = model_diagram(model_with_report)
    assert any("cycle" in note for note in diagram.notes)


# -- the model diagram --------------------------------------------------------------------------


def test_tables_and_measures_are_both_nodes(model):
    kinds = {node.kind for node in model.nodes}
    assert kinds == {"table", "measure"}
    assert {n.label for n in model.nodes if n.kind == "table"} >= {"Sales", "Product"}


def test_relationships_are_drawn_in_the_direction_filters_travel(model):
    """Dimension into fact, which is how a star schema is read."""
    relationship = next(e for e in model.edges if e.kind == "relationship")
    assert (relationship.source, relationship.target) == ("Product", "Sales")


def test_a_measure_is_fed_by_the_tables_its_columns_belong_to(model):
    feeders = {e.source for e in model.edges if e.target == "[Total Sales]"}
    assert "Sales" in feeders


def test_measure_to_measure_edges_survive_the_rollup(model):
    assert any(
        e.source == "[Total Sales]" and e.target == "[Margin % (old)]" for e in model.edges
    )


def test_measures_sit_right_of_the_tables(model):
    tables = [model.tiers[n.id] for n in model.nodes if n.kind == "table"]
    measures = [model.tiers[n.id] for n in model.nodes if n.kind == "measure"]
    assert min(measures) > min(tables)


def test_an_auto_date_table_is_muted(model):
    auto = next(n for n in model.nodes if n.label.startswith("LocalDateTable"))
    assert auto.muted


def test_a_table_carries_its_shape_in_the_subtitle(model_with_report):
    """With metrics the subtitle is size; without, at least the storage mode."""
    sales = next(n for n in model_diagram(model_with_report).nodes if n.label == "Sales")
    assert sales.detail.startswith("4 column(s)")


# -- the workspace diagram -----------------------------------------------------------------------


def test_models_and_reports_are_both_nodes(workspace):
    assert {n.kind for n in workspace.nodes} == {"model", "report"}


def test_a_report_hangs_off_its_model(workspace):
    assert any(
        e.source == "model:Contoso" and e.target == "report:Exec.Report"
        for e in workspace.edges
    )


def test_an_unmatched_report_is_not_put_in_the_models_column(workspace):
    """It has no edges, so layering would otherwise leave it at tier 0 beside the models."""
    stray = next(n for n in workspace.nodes if n.label == "Stray.Report")
    assert workspace.tiers[stray.id] >= 1
    assert stray.muted
    assert workspace.tier_labels[0] == "MODELS"


def test_free_floating_reports_are_noted(workspace):
    assert any("float free" in note for note in workspace.notes)


def test_an_orphan_model_is_muted(workspace):
    orphan = next(n for n in workspace.nodes if n.label == "Orphan")
    assert orphan.muted
    assert "no report in this scan references it" in orphan.detail


# -- rendering----------------------------------------------------------------------------------


def test_the_svg_is_well_formed(model):
    xml.dom.minidom.parseString(svg_of(render_diagram(model)))


def test_every_node_and_edge_is_addressable(model):
    block = render_diagram(model)
    assert block.count('class="dg-node"') == len(model.nodes)
    assert block.count('class="dg-edge"') == len(model.edges)
    assert 'data-id="Sales"' in block


def test_the_adjacency_is_embedded_for_offline_highlighting(model):
    """Highlighting must not need a server; a saved file behaves the same as `serve`."""
    block = render_diagram(model)
    payload = re.search(r"const near = (\{.*?\});", block, re.S).group(1)
    near = json.loads(payload.replace("\\u003c", "<").replace("\\u003e", ">"))
    assert "[Total Sales]" in near["Sales"]
    assert "Sales" in near["[Total Sales]"]  # both directions, so a route can be walked


def test_the_embedded_adjacency_cannot_close_its_script(model_with_report):
    nasty = model_with_report.measures["[Total Sales]"]
    nasty.name = "</script><img src=x onerror=alert(1)>"
    model_with_report.measures[nasty.key] = nasty
    block = render_diagram(model_diagram(model_with_report))
    scripts = re.findall(r"<script>(.*?)</script>", block, re.S)
    assert scripts, "the behaviour script was truncated by an unescaped name"
    assert "const near" in scripts[-1]


def test_node_labels_are_escaped(model_with_report):
    nasty = model_with_report.measures["[Total Sales]"]
    nasty.name = "A & B <b>"
    model_with_report.measures[nasty.key] = nasty
    svg = svg_of(render_diagram(model_diagram(model_with_report)))
    xml.dom.minidom.parseString(svg)
    assert "<b>" not in svg


def test_a_crowded_tier_wraps_rather_than_truncating(model_with_report):
    """Nothing is dropped; the tier becomes two columns."""
    from dax_quax.model import Measure

    for index in range(WRAP_AT + 4):
        measure = Measure(table="Sales", name=f"M{index}", expression="SUM ( Sales[Quantity] )")
        model_with_report.measures[measure.key] = measure
    diagram = model_diagram(model_with_report)
    block = render_diagram(diagram)
    assert block.count('class="dg-node"') == len(diagram.nodes)


def test_an_empty_diagram_says_so():
    from dax_quax.analysis.diagram import Diagram

    assert "Nothing to draw" in render_diagram(Diagram(title="x"))


def test_the_legend_lists_only_the_kinds_present(model, workspace):
    assert ">table<" in render_diagram(model)
    assert ">model<" not in render_diagram(model)
    assert ">model<" in render_diagram(workspace)


# -- wired into both pages----------------------------------------------------------------------


def test_the_model_page_carries_its_diagram(model_with_report, tmp_path):
    from dax_quax.render.report import render_report

    html = render_report(model_with_report, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "HOW THIS MODEL FITS TOGETHER" in html
    assert 'id="model-diagram"' in html
    assert "click a node to highlight" in html


def test_the_workspace_index_carries_its_diagram(tmp_path):
    from dax_quax.render.workspace import render_workspace

    index = render_workspace(discover_workspace(WS), tmp_path / "out")
    html = index.read_text(encoding="utf-8")
    assert "HOW THIS WORKSPACE FITS TOGETHER" in html
    assert 'id="ws-diagram"' in html


def test_a_diagram_failure_does_not_cost_the_report(model_with_report, tmp_path, monkeypatch):
    """A picture is not worth the page it sits on."""
    import dax_quax.render.report as module

    monkeypatch.setattr(
        module, "render_diagram", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    html = render_report(model_with_report, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "could not be drawn" in html
    assert "Order Number" in html  # the rest of the report is intact

