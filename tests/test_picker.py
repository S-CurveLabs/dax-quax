"""Choosing the source from the `serve` page: the picker's rules, then the switchboard."""

from __future__ import annotations

import pathlib

import pytest

from dax_quax.cli import main
from dax_quax.errors import DaxQuaxError
from dax_quax.render import picker

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
WORKSPACE = FIXTURES / "synthetic_workspace"
PROJECT = FIXTURES / "synthetic_pbip"


# -- what a folder holds -----------------------------------------------------------------


def test_a_listing_names_what_each_entry_is():
    listed = picker.listing(WORKSPACE)
    kinds = {e["name"]: e["kind"] for e in listed["entries"]}
    assert kinds["Contoso.SemanticModel"] == "model"
    assert kinds["Exec.Report"] == "report"
    assert kinds["Contoso.pbip"] == "pbip"
    assert listed["models_here"] == 3
    assert listed["scannable"] is True


def test_folders_come_before_files(tmp_path):
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha.pbix").write_bytes(b"")
    names = [e["name"] for e in picker.listing(tmp_path)["entries"]]
    assert names == ["zeta", "alpha.pbix"]


def test_hidden_and_irrelevant_entries_are_left_out(tmp_path):
    for name in (".git", "$Recycle.Bin", "keep"):
        (tmp_path / name).mkdir()
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "Model.PBIX").write_bytes(b"")
    names = {e["name"] for e in picker.listing(tmp_path)["entries"]}
    assert names == {"keep", "Model.PBIX"}


def test_a_listing_of_something_that_is_not_a_folder_raises(tmp_path):
    with pytest.raises(DaxQuaxError, match="not a folder"):
        picker.listing(tmp_path / "missing")


def test_a_drive_root_and_the_home_folder_are_not_scannable():
    home = pathlib.Path.home()
    assert picker.listing(home)["scannable"] is False
    assert picker.listing(home.anchor)["scannable"] is False


def test_a_long_folder_is_cut_short_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(picker, "MAX_ENTRIES", 3)
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
    listed = picker.listing(tmp_path)
    assert len(listed["entries"]) == 3
    assert listed["truncated"] is True


# -- what a pick means -------------------------------------------------------------------


@pytest.mark.parametrize("where", [lambda: pathlib.Path.home(), lambda: pathlib.Path.home().anchor])
def test_a_workspace_that_would_walk_the_whole_disk_is_refused(where):
    with pytest.raises(DaxQuaxError, match="every folder under it"):
        picker.choose("workspace", str(where()))


def test_a_folder_is_a_workspace():
    choice = picker.choose("workspace", str(WORKSPACE))
    assert choice.shape == "workspace"
    assert set(choice.loader().models) == {"Contoso", "Orphan", "Regional"}


def test_a_lone_project_is_a_model_with_its_reports():
    choice = picker.choose("project", str(PROJECT / "Contoso.pbip"))
    assert choice.shape == "model"
    assert choice.loader().name


def test_a_project_beside_another_model_is_refused():
    """open_pbip attaches every sibling report, which here includes other models'."""
    with pytest.raises(DaxQuaxError, match="Scan the folder"):
        picker.choose("project", str(WORKSPACE / "Contoso.SemanticModel"))


@pytest.mark.parametrize(
    ("kind", "target", "message"),
    [
        ("file", PROJECT / "Contoso.pbip", "not a .pbix"),
        ("project", PROJECT / "Contoso.Report", "not a .pbip file"),
        ("workspace", PROJECT / "Contoso.pbip", "not a folder"),
        ("teleport", PROJECT, "unknown kind"),
    ],
)
def test_a_pick_of_the_wrong_kind_says_what_was_expected(kind, target, message):
    with pytest.raises(DaxQuaxError, match=message):
        picker.choose(kind, str(target))


def test_a_path_is_required_except_for_a_live_connection():
    with pytest.raises(DaxQuaxError, match="needs a path"):
        picker.choose("workspace")
    assert picker.choose("live", port=51234).description == "localhost:51234"


# -- the switchboard ---------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from dax_quax.render.serve import create_app, create_switchboard  # noqa: E402


def _client(board) -> TestClient:
    return TestClient(board, base_url="http://127.0.0.1")


@pytest.fixture
def board():
    return create_switchboard(start=str(WORKSPACE))


def test_with_nothing_loaded_the_root_goes_to_the_picker(board):
    client = _client(board)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/open"
    page = client.get("/open")
    assert page.status_code == 200
    assert "choose what to scan" in page.text
    assert str(WORKSPACE) in page.text


def test_browse_lists_a_folder_and_rejects_a_missing_one(board):
    client = _client(board)
    listed = client.get("/api/browse", params={"path": str(WORKSPACE)}).json()
    assert listed["models_here"] == 3
    missing = client.get("/api/browse", params={"path": str(WORKSPACE / "nope")})
    assert missing.status_code == 400
    assert "not a folder" in missing.json()["error"]


def test_browse_starts_where_serve_was_started(board):
    assert _client(board).get("/api/browse").json()["path"] == str(WORKSPACE.resolve())


def test_picking_a_folder_serves_it_as_a_workspace(board):
    client = _client(board)
    opened = client.post("/api/open", json={"kind": "workspace", "path": str(WORKSPACE)})
    assert opened.status_code == 204
    index = client.get("/")
    assert index.status_code == 200
    assert "workspace scan" in index.text
    assert 'href="/open"' in index.text  # the way back to the picker
    assert client.get("/model/contoso/").status_code == 200


def test_a_failed_pick_keeps_the_source_you_had(board):
    client = _client(board)
    project = {"kind": "project", "path": str(PROJECT / "Contoso.pbip")}
    assert client.post("/api/open", json=project).status_code == 204
    bad = client.post("/api/open", json={"kind": "file", "path": str(PROJECT / "missing.pbix")})
    assert bad.status_code == 400
    assert "nothing at" in bad.json()["error"]
    assert board.description == str((PROJECT / "Contoso.pbip").resolve())
    assert client.get("/").status_code == 200


def test_a_folder_with_no_models_is_refused(board, tmp_path):
    response = _client(board).post("/api/open", json={"kind": "workspace", "path": str(tmp_path)})
    assert response.status_code == 400
    assert "no semantic models" in response.json()["error"]
    assert board.current is None


def test_a_source_that_fails_to_load_is_refused_with_its_error(board, tmp_path):
    empty_model = tmp_path / "Broken.SemanticModel"
    empty_model.mkdir()
    response = _client(board).post("/api/open", json={"kind": "project", "path": str(empty_model)})
    assert response.status_code == 400
    assert response.json()["error"]
    assert board.current is None


def test_the_picker_refuses_a_cross_site_post(board):
    response = _client(board).post(
        "/api/open",
        json={"kind": "workspace", "path": str(WORKSPACE)},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403
    assert board.current is None


def test_the_picker_refuses_a_foreign_host(board):
    """DNS rebinding: another site's name pointed at 127.0.0.1 must not read the disk."""
    client = TestClient(board, base_url="http://attacker.example")
    assert client.get("/api/browse", params={"path": str(WORKSPACE)}).status_code == 400


def test_a_page_served_without_a_picker_has_no_change_source_link():
    from dax_quax.sources.pbip import open_pbip

    client = TestClient(create_app(lambda: open_pbip(PROJECT)), base_url="http://127.0.0.1")
    assert 'href="/open"' not in client.get("/").text


# -- the command line --------------------------------------------------------------------


def test_bare_serve_opens_the_picker(monkeypatch):
    import dax_quax.render.serve as serve_module

    seen = {}
    monkeypatch.setattr(
        serve_module, "serve_switchboard", lambda initial, **kw: seen.update(kw, initial=initial)
    )
    assert main(["serve", "--no-browser"]) == 0
    assert seen["initial"] is None
    assert seen["start"] == str(pathlib.Path.cwd())


def test_the_picker_is_never_offered_off_localhost():
    from dax_quax.render.serve import serve_switchboard

    with pytest.raises(DaxQuaxError, match="only offered on localhost"):
        serve_switchboard(None, host="0.0.0.0", open_browser=False)


def test_a_given_source_off_localhost_is_served_without_the_picker(monkeypatch):
    import dax_quax.render.serve as serve_module

    seen = {}
    monkeypatch.setattr(serve_module, "serve", lambda loader, **kw: seen.update(kw))
    serve_module.serve_switchboard(
        ("model", lambda: None, "x"), host="0.0.0.0", open_browser=False
    )
    assert seen["host"] == "0.0.0.0"
