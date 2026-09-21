"""Four ways the tool could be turned against the person running it.

None of these is a verdict bug, but all four share the report's shape: the thing that
makes the tool useful — reading a model's names, serving them on localhost, downloading
the client assemblies — is also the thing that carries the risk.

    a Host header that is not ours       DNS rebinding reads the whole model cross-origin
    a cross-site POST                    /api/rescan needs no rebinding to be called
    a quote in an object's name          breaks out of an SVG attribute
    a backslash in a zip entry           writes a DLL outside the runtime folder
"""

from __future__ import annotations

import io
import pathlib
import zipfile
from xml.etree import ElementTree

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from dax_quax.render.serve import create_app  # noqa: E402
from dax_quax.sources.pbip import open_pbip  # noqa: E402

PBIP = pathlib.Path(__file__).parent / "fixtures" / "synthetic_pbip"


@pytest.fixture
def app():
    return create_app(lambda: open_pbip(PBIP), description="fixture")


# -- the served app --------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_loopback_host_is_served(app, host):
    assert TestClient(app, base_url=f"http://{host}").get("/").status_code == 200


def test_a_foreign_host_header_is_refused(app):
    """A rebound DNS name resolves here but still says who it is in the Host header."""
    response = TestClient(app, base_url="http://attacker.example").get("/")
    assert response.status_code == 400
    assert "Order Number" not in response.text


def test_a_cross_site_rescan_is_refused(app):
    client = TestClient(app, base_url="http://127.0.0.1")
    refused = client.post("/api/rescan", headers={"sec-fetch-site": "cross-site"})
    assert refused.status_code == 403


def test_the_pages_own_rescan_still_works(app):
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.post("/api/rescan", headers={"sec-fetch-site": "same-origin"}).status_code == 204
    # No header at all is curl, not a browser, and the Host check is what covers it.
    assert client.post("/api/rescan").status_code == 204


# -- SVG attributes --------------------------------------------------------------------------


def test_a_quote_in_a_name_cannot_open_an_attribute(synthetic_model):
    """`escape` from the standard library leaves `"` alone, and every name lands in one."""
    from dax_quax.analysis.lineage import build_lineage
    from dax_quax.model import Column
    from dax_quax.render.graph import render_lineage_svg

    hostile = 'Evil" onload="alert(1)'
    column = Column(table=hostile, name="C")
    synthetic_model.columns[column.key] = column
    svg = render_lineage_svg(build_lineage(synthetic_model), column.key)
    assert "&quot; onload=&quot;" in svg
    # The proof is what a parser sees: the name stayed inside aria-label and opened no
    # second attribute. Element *text* needs no quote escaping and is left alone.
    root = ElementTree.fromstring(svg)
    assert root.get("aria-label") == f"Lineage around {column.key}"
    assert root.get("onload") is None


def test_a_quote_in_a_diagram_node_cannot_open_an_attribute():
    from dax_quax.analysis.diagram import Diagram, DiagramEdge, DiagramNode
    from dax_quax.render.diagram import render_diagram

    hostile = 'x" onclick="alert(1)'
    diagram = Diagram(
        title=hostile,
        nodes=[
            DiagramNode(id=hostile, label="a", kind="table"),
            DiagramNode(id="b", label="b", kind="table"),
        ],
        edges=[DiagramEdge(source=hostile, target="b", kind="expression")],
    )
    html = render_diagram(diagram)
    assert '" onclick="' not in html
    assert "&quot; onclick=&quot;" in html


# -- unpacking the runtime ---------------------------------------------------------------------


def _nupkg(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_an_entry_that_escapes_the_destination_is_not_written(monkeypatch, tmp_path):
    """A backslash is a path separator on Windows and passes the locale-subfolder test."""
    from dax_quax import runtime

    package = _nupkg(
        {
            "lib/netcoreapp3.0/Good.dll": b"ok",
            "lib/netcoreapp3.0/..\\..\\evil.dll": b"no",
            "lib/netcoreapp3.0/de/Locale.dll": b"no",
        }
    )
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *a, **k: _Response(package))

    dest = tmp_path / "runtime"
    taken = runtime.fetch_package("pkg", "1.0.0", dest)

    assert taken == ["Good.dll"]
    assert not (tmp_path / "evil.dll").exists()
    assert sorted(p.name for p in dest.iterdir()) == ["Good.dll"]


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# -- where the runtime config is written ---------------------------------------------------------


def test_the_runtime_config_is_not_written_to_shared_temp(monkeypatch, tmp_path):
    """TEMP is shared between users; this file decides which runtime coreclr boots."""
    from dax_quax.sources.live import _runtime_config

    monkeypatch.setenv("TEMP", str(tmp_path / "shared"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "mine"))

    config = _runtime_config(tmp_path / "nowhere" / "libs")
    assert config.is_file()
    assert (tmp_path / "mine") in config.parents
    assert not (tmp_path / "shared").exists()


def test_a_config_shipped_beside_the_assemblies_wins(tmp_path):
    """A wheel or fetch_libs.py may vendor one; it is trusted and not overwritten."""
    from dax_quax.sources.live import _runtime_config

    libs = tmp_path / "pkg" / "_libs"
    libs.mkdir(parents=True)
    beside = tmp_path / "pkg" / "daxquax.runtimeconfig.json"
    beside.write_text("{}", encoding="utf-8")
    assert _runtime_config(libs) == beside
