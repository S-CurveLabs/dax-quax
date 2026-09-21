"""How a report says which published semantic model it belongs to.

The last of the six contracts §10 listed as declared-but-unverified. Settled on 2026-09-18
against the published PBIR schema rather than against an engine, because no report in the
borrowed corpus binds this way -- every one of them uses `byPath`.

WHAT WAS DECLARED, AND WHY IT COULD NOT WORK
--------------------------------------------
The old code looked for the dataset's name in five sibling fields of `byConnection`:
pbiModelDatabaseName, pbiModelDatabaseId, datasetId, database, name.

In the current schema (definitionProperties 2.0.0) `byConnection` holds **only**
`connectionString`, with `additionalProperties: false`. So none of the five exist, and a
modern live-connected report fell through to "names neither a path nor a connection".

In the older 1.0.0 schema the fields do exist, and two of them are traps:
`pbiModelDatabaseName` is the semantic model **id**, not its name, and `name` is the
constant "EntityDataSource". Matching a model name against either could only succeed by
accident.

The dataset's name is inside the connection string, as `initial catalog`.
"""

from __future__ import annotations

import json
import shutil

import pytest

from dax_quax.sources.workspace import discover_workspace, parse_connection_string

WS = "tests/fixtures/synthetic_workspace"

V2 = (
    'Data Source="powerbi://api.powerbi.com/v1.0/myorg/Sales Ops";'
    "initial catalog=Contoso;access mode=readonly;integrated security=ClaimsToken;"
    "semanticmodelid=8c1d77ae-4b20-4f0e-9a6c-1e5d3b77c902"
)
V1 = (
    "Data Source=powerbi://api.powerbi.com/v1.0/myorg/Finance;"
    "Initial Catalog=Contoso;Integrated Security=ClaimsToken"
)


@pytest.fixture
def workspace():
    return discover_workspace(WS)


def link_for(workspace, name):
    return next(link for link in workspace.links if link.name == name)


# -- the connection string ---------------------------------------------------------------


def test_the_2_0_0_form_parses():
    parsed = parse_connection_string(V2)
    assert parsed["initial catalog"] == "Contoso"
    assert parsed["semanticmodelid"] == "8c1d77ae-4b20-4f0e-9a6c-1e5d3b77c902"
    assert parsed["data source"] == "powerbi://api.powerbi.com/v1.0/myorg/Sales Ops"


def test_the_1_0_0_form_parses():
    parsed = parse_connection_string(V1)
    assert parsed["initial catalog"] == "Contoso"
    assert "semanticmodelid" not in parsed


def test_keys_are_case_folded():
    """2.0.0 writes 'initial catalog', 1.0.0 writes 'Initial Catalog'."""
    assert parse_connection_string("Initial Catalog=X")["initial catalog"] == "X"
    assert parse_connection_string("INITIAL CATALOG=X")["initial catalog"] == "X"


def test_a_quoted_value_may_contain_a_semicolon():
    """A workspace name with a semicolon would otherwise be cut in half."""
    parsed = parse_connection_string('Data Source="powerbi://x/a;b";initial catalog=C')
    assert parsed["data source"] == "powerbi://x/a;b"
    assert parsed["initial catalog"] == "C"


def test_an_empty_string_parses_to_nothing():
    assert parse_connection_string("") == {}


def test_a_value_containing_an_equals_sign_survives():
    assert parse_connection_string("k=a=b")["k"] == "a=b"


# -- matching ------------------------------------------------------------------------------


def test_a_1_0_0_report_matches_on_its_initial_catalog(workspace):
    """Regional.Report carries all six legacy fields; only the catalog names the model."""
    link = link_for(workspace, "Regional.Report")
    assert link.by_connection == "Regional"
    assert link.model == "Regional"


def test_the_legacy_id_field_is_not_mistaken_for_a_name(workspace):
    """pbiModelDatabaseName is the model's id. It is recorded as one."""
    link = link_for(workspace, "Regional.Report")
    assert link.model_id == "5f9a0c31-2d4e-4a77-bd10-9c2e6b83aa41"
    assert link.by_connection != link.model_id


def test_a_2_0_0_report_with_no_local_model_is_unmatched(workspace):
    link = link_for(workspace, "Stray.Report")
    assert not link.matched
    assert "Warehouse Finance" in link.reason
    assert "published only, or renamed" in link.reason


def test_an_unmatched_connection_names_the_workspace_it_points_at(workspace):
    """Which tenant workspace to go and look in, rather than just 'not here'."""
    link = link_for(workspace, "Stray.Report")
    assert link.workspace == "Warehouse Ops"
    assert "Warehouse Ops" in link.reason


def test_the_model_id_is_read_from_the_connection_string(workspace):
    link = link_for(workspace, "Stray.Report")
    assert link.model_id == "8c1d77ae-4b20-4f0e-9a6c-1e5d3b77c902"


def test_matching_is_case_insensitive(tmp_path):
    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    pbir = copy / "Regional.Report" / "definition.pbir"
    document = json.loads(pbir.read_text(encoding="utf-8"))
    document["datasetReference"]["byConnection"]["connectionString"] = (
        "Data Source=powerbi://api.powerbi.com/v1.0/myorg/A;Initial Catalog=rEgIoNaL"
    )
    pbir.write_text(json.dumps(document), encoding="utf-8")
    assert link_for(discover_workspace(copy), "Regional.Report").model == "Regional"


# -- the shape that names nothing -----------------------------------------------------------


def test_a_rest_api_deployment_has_no_catalog_to_match_on(tmp_path):
    """`{"connectionString": "semanticmodelid=..."}` is what the Fabric REST API writes.

    There is no name in it, so the model cannot be identified. Saying that plainly is the
    whole job: it degrades every verdict, which is correct, and it says why.
    """
    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    pbir = copy / "Regional.Report" / "definition.pbir"
    pbir.write_text(
        json.dumps(
            {
                "version": "4.0",
                "datasetReference": {
                    "byConnection": {"connectionString": "semanticmodelid=abc-123"}
                },
            }
        ),
        encoding="utf-8",
    )
    link = link_for(discover_workspace(copy), "Regional.Report")
    assert not link.matched
    assert link.model_id == "abc-123"
    assert "no dataset name" in link.reason
    assert "abc-123" in link.reason


def test_an_empty_byconnection_is_reported_not_crashed(tmp_path):
    copy = tmp_path / "ws"
    shutil.copytree(WS, copy)
    (copy / "Regional.Report" / "definition.pbir").write_text(
        json.dumps({"version": "4.0", "datasetReference": {"byConnection": {}}}),
        encoding="utf-8",
    )
    link = link_for(discover_workspace(copy), "Regional.Report")
    assert not link.matched
    assert link.reason


# -- the consequence -------------------------------------------------------------------------


def test_an_unmatched_connection_still_degrades_every_verdict(workspace):
    """§14's asymmetry rule. A report that could not be tied to a model is usage not counted."""
    assert workspace.unmatched
    for name in workspace.models:
        assert not workspace.scope_for(name).trustworthy
        assert all(
            str(f.verdict) != "REMOVE" for f in workspace.findings_for(name)
        ), f"{name} claims a removable object while a report is unmatched"
