"""Lineage rendering and the served app."""

from __future__ import annotations

import xml.dom.minidom

import pytest

from dax_quax.render.graph import render_lineage_svg, tiered_layout
from dax_quax.render.serve import State, create_app

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

PBIP = "tests/fixtures/synthetic_pbip"


@pytest.fixture
def lineage(model_with_report):
    return model_with_report.lineage()


# -- layout -----------------------------------------------------------------------------------


def test_tiers_are_hop_distance(lineage):
    layout = tiered_layout(lineage, "[Total Sales]", radius=2)
    assert layout.nodes["[Total Sales]"].tier == 0
    assert layout.nodes["Sales[Line Amount]"].tier == -1
    assert layout.nodes["Sales[Quantity]"].tier == -2
    assert layout.nodes["[Margin % (old)]"].tier == 1


def test_radius_bounds_the_picture(lineage):
    near = tiered_layout(lineage, "[Total Sales]", radius=1)
    far = tiered_layout(lineage, "[Total Sales]", radius=3)
    assert "Sales[Quantity]" not in near.nodes
    assert "Sales[Quantity]" in far.nodes


def test_direction_filters_the_walk(lineage):
    up = tiered_layout(lineage, "[Total Sales]", radius=2, direction="up")
    down = tiered_layout(lineage, "[Total Sales]", radius=2, direction="down")
    assert all(node.tier <= 0 for node in up.nodes.values())
    assert all(node.tier >= 0 for node in down.nodes.values())


def test_tiers_are_ordered_left_to_right(lineage):
    layout = tiered_layout(lineage, "[Total Sales]", radius=2)
    ordered = sorted(layout.tiers.items())
    assert [x for _, x in ordered] == sorted(x for _, x in ordered)


def test_edges_are_restricted_to_drawn_nodes(lineage):
    layout = tiered_layout(lineage, "[Total Sales]", radius=1)
    for source, target in layout.edges:
        assert source in layout.nodes
        assert target in layout.nodes


def test_focus_node_is_larger_and_marked(lineage):
    layout = tiered_layout(lineage, "[Total Sales]", radius=1)
    focus = layout.nodes["[Total Sales]"]
    assert focus.is_focus
    assert focus.h > layout.nodes["Sales[Line Amount]"].h


def test_an_unknown_key_draws_nothing(lineage):
    assert tiered_layout(lineage, "Nope[Nope]").nodes == {}


def test_a_crowded_tier_is_truncated_and_says_so(lineage, model_with_report):
    """An unbounded tier would render as unreadable spaghetti, which is why radius exists."""
    from dax_quax.model import Measure

    for index in range(12):
        measure = Measure(table="Sales", name=f"Derived {index}", expression="[Total Sales] + 1")
        model_with_report.measures[measure.key] = measure
    layout = tiered_layout(model_with_report.lineage(), "[Total Sales]", radius=1)
    assert layout.truncated > 0
    assert "not drawn" in render_lineage_svg(model_with_report.lineage(), "[Total Sales]", 1)


# -- svg --------------------------------------------------------------------------------------


def test_svg_is_well_formed_xml(lineage):
    svg = render_lineage_svg(lineage, "[Total Sales]", radius=2)
    xml.dom.minidom.parseString(svg)  # raises if malformed
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_svg_labels_the_tiers(lineage):
    svg = render_lineage_svg(lineage, "[Total Sales]", radius=2)
    assert "FOCUS" in svg
    assert "-1 HOP" in svg
    assert "+1 HOP" in svg


def test_svg_escapes_object_names(model_with_report):
    """Names go straight into markup, so a bracket or ampersand must not break it."""
    from dax_quax.model import Measure

    nasty = Measure(table="Sales", name='A & B <script>', expression="[Total Sales] + 1")
    model_with_report.measures[nasty.key] = nasty
    svg = render_lineage_svg(model_with_report.lineage(), nasty.key, radius=1)
    xml.dom.minidom.parseString(svg)
    assert "<script>" not in svg
    assert "&amp;" in svg


def test_an_isolated_object_draws_as_a_lone_node(model_with_report):
    """The REMOVE case. Nothing points at it and it points at nothing, and the picture
    should show exactly that rather than refusing to draw."""
    lineage = model_with_report.lineage()
    layout = tiered_layout(lineage, "Sales[Order Number]", radius=2)
    assert set(layout.nodes) == {"Sales[Order Number]"}
    assert layout.edges == []
    svg = render_lineage_svg(lineage, "Sales[Order Number]", radius=2)
    xml.dom.minidom.parseString(svg)
    assert "FOCUS" in svg


def test_a_key_that_is_not_in_the_model_says_so(model_with_report):
    assert "no lineage" in render_lineage_svg(model_with_report.lineage(), "Gone[Gone]")


# -- state ---------------------------------------------------------------------------------------


def test_rescan_reloads_from_the_source():
    from dax_quax.sources.pbip import open_pbip

    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return open_pbip(PBIP)

    state = State(loader=loader)
    state.rescan()
    state.rescan()
    assert calls["n"] == 2
    assert state.scans == 2
    assert state.model is not None


def test_a_failing_loader_is_reported_not_raised():
    """A model that will not load must show an error, not take the server down."""

    def loader():
        raise RuntimeError("desktop closed")

    state = State(loader=loader)
    state.rescan()
    assert state.model is None
    assert "desktop closed" in state.error


# -- the app -------------------------------------------------------------------------------------


@pytest.fixture
def client():
    from dax_quax.sources.pbip import open_pbip

    return TestClient(
        create_app(lambda: open_pbip(PBIP), description="fixture"),
        base_url="http://127.0.0.1",
    )


def test_index_renders_the_same_page(client):
    body = client.get("/").text
    assert "WHAT THIS SCAN COULD NOT SEE" in body
    assert "Order Number" in body
    assert 'id="rescan"' in body  # served mode only


def test_the_written_file_has_no_rescan_button(model_with_report, tmp_path):
    from dax_quax.render.report import render_report

    html = render_report(model_with_report, tmp_path / "r.html").read_text(encoding="utf-8")
    assert 'id="rescan"' not in html
    assert "const SERVED = false" in html


def test_lineage_endpoint_returns_svg(client):
    response = client.get("/api/lineage", params={"key": "[Total Sales]", "radius": 2})
    assert response.status_code == 200
    xml.dom.minidom.parseString(response.text)


def test_lineage_endpoint_honours_radius_and_direction(client):
    near = client.get("/api/lineage", params={"key": "[Total Sales]", "radius": 1}).text
    far = client.get("/api/lineage", params={"key": "[Total Sales]", "radius": 3}).text
    assert len(far) > len(near)
    up = client.get(
        "/api/lineage", params={"key": "[Total Sales]", "direction": "up"}
    ).text
    assert "+1 HOP" not in up


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"key": "x", "radius": 99}, 422),
        ({"key": "x", "direction": "sideways"}, 422),
        ({}, 422),
    ],
)
def test_lineage_endpoint_rejects_bad_input(client, params, expected):
    assert client.get("/api/lineage", params=params).status_code == expected


def test_rescan_endpoint(client):
    assert client.post("/api/rescan").status_code == 204


def test_rescan_failure_is_a_500_not_a_crash():
    calls = {"n": 0}

    def loader():
        from dax_quax.sources.pbip import open_pbip

        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("source vanished")
        return open_pbip(PBIP)

    client = TestClient(create_app(loader), base_url="http://127.0.0.1")
    assert client.get("/").status_code == 200
    assert client.post("/api/rescan").status_code == 500
    # the previously loaded model is still being served
    assert client.get("/").status_code == 200


def test_served_page_escapes_a_hostile_model_name():
    from dax_quax.sources.pbip import open_pbip

    def loader():
        model = open_pbip(PBIP)
        model.name = '<script>alert(1)</script>'
        return model

    body = TestClient(create_app(loader), base_url="http://127.0.0.1").get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_subtitles_are_not_clipped_early(lineage):
    """The two label lines use different faces, so they need different width budgets.

    One shared figure clipped "Sales / calc column" to "Sales / calc colu...".
    """
    svg = render_lineage_svg(lineage, "[Total Sales]", radius=2)
    assert "calc column" in svg
    assert "calc colu…" not in svg
