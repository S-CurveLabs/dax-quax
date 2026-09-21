"""Serving a whole directory, rather than one model.

The index and the per-model pages are the same two templates `report --workspace` writes.
What is new is that a model page is no longer at the root, which is where a served page
had always quietly assumed it was.
"""

from __future__ import annotations

import pytest

from dax_quax.render.serve import WorkspaceState, create_workspace_app
from dax_quax.render.workspace import slug_map, slugify
from dax_quax.sources.workspace import discover_workspace

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

WS = "tests/fixtures/synthetic_workspace"


@pytest.fixture
def client():
    return TestClient(
        create_workspace_app(lambda: discover_workspace(WS), description=WS),
        base_url="http://127.0.0.1",
    )


@pytest.fixture
def state():
    state = WorkspaceState(loader=lambda: discover_workspace(WS))
    state.ensure()
    return state


# -- slugs must be distinct ------------------------------------------------------------


def test_slugs_are_distinct_even_when_names_fold_together():
    """`slugify` drops case and punctuation, so two names can land on one page."""
    names = ["Sales EU", "sales-eu", "Sales_EU"]
    slugs = slug_map(names)
    assert len({*slugs.values()}) == 3
    assert slugify("Sales EU") in slugs.values()


def test_a_slug_map_is_stable_across_calls():
    names = ["Beta", "Alpha", "Gamma"]
    assert slug_map(names) == slug_map(reversed(names))


# -- the index -------------------------------------------------------------------------


def test_the_index_is_the_workspace_page(client):
    html = client.get("/").text
    assert "workspace scan" in html
    assert "Contoso" in html


def test_the_index_links_to_served_routes_not_files(client):
    html = client.get("/").text
    assert '.html"' not in html
    assert 'href="/model/contoso/"' in html


def test_the_index_still_says_what_it_could_not_match(client):
    """The degrade is the whole point of a workspace scan; serving must not lose it."""
    html = client.get("/").text
    assert "Stray.Report" in html
    assert "unmatched" in html.lower()


# -- a model page --------------------------------------------------------------------


def test_a_model_page_renders(client):
    html = client.get("/model/contoso/").text
    assert "model cost and usage" in html
    assert "Contoso" in html


def test_a_model_page_links_back_to_the_index(client):
    assert 'href="/"' in client.get("/model/contoso/").text


def test_a_model_page_points_its_api_at_itself(client):
    """A relative "api/" would resolve under /model/<slug>/ and 404 on the real thing."""
    html = client.get("/model/contoso/").text
    assert '"/model/contoso/api/"' in html


def test_a_model_page_carries_the_workspace_scope(client):
    """Not the model's own scope: the workspace knows about reports the model never saw."""
    workspace = discover_workspace(WS)
    assert workspace.scope_for("Contoso").describe() in client.get("/model/contoso/").text


def test_an_unknown_model_is_a_404(client):
    assert client.get("/model/nope/").status_code == 404


# -- the lineage endpoint ----------------------------------------------------------------


def test_lineage_is_drawn_for_the_right_model(client):
    response = client.get(
        "/model/contoso/api/lineage", params={"key": "[Total Sales]", "radius": 2}
    )
    assert response.status_code == 200
    assert "<svg" in response.text


def test_lineage_under_an_unknown_model_is_a_404(client):
    response = client.get("/model/nope/api/lineage", params={"key": "x"})
    assert response.status_code == 404


def test_lineage_rejects_a_radius_that_would_draw_the_whole_graph(client):
    response = client.get(
        "/model/contoso/api/lineage", params={"key": "[Total Sales]", "radius": 99}
    )
    assert response.status_code == 422


# -- rescan --------------------------------------------------------------------------


def test_rescan_re_reads_the_directory(client):
    assert client.post("/api/rescan").status_code == 204


def test_a_model_pages_rescan_re_reads_the_whole_workspace(client):
    """A model alone cannot say another report started using one of its columns."""
    client.get("/model/contoso/")
    state = client.app.state.dax_quax
    before = state.scans
    assert client.post("/model/contoso/api/rescan").status_code == 204
    assert state.scans == before + 1


def test_a_failed_rescan_is_shown_rather_than_raised():
    """A person can see a broken page. Serving a traceback instead helps nobody."""
    state = WorkspaceState(loader=lambda: 1 / 0)
    state.rescan()
    assert "ZeroDivisionError" in state.error
    assert state.workspace is None


def test_a_rescan_drops_the_cached_lineages(state):
    state.lineage_for("Contoso")
    assert state.lineages
    state.rescan()
    assert state.lineages == {}


def test_lineages_are_built_once_and_kept(state):
    """A workspace scan is expensive; a served graph must not rebuild it per request."""
    first = state.lineage_for("Contoso")
    assert state.lineage_for("Contoso") is first


# -- the CLI ---------------------------------------------------------------------------


def test_the_cli_refuses_a_report_folder_over_a_workspace(capsys):
    """Adding one by hand counts it for one model and hides it from every other."""
    from dax_quax.cli import main

    assert main(["serve", "--workspace", WS, "--report-folder", WS]) == 1
    assert "already matched to its model" in capsys.readouterr().err
