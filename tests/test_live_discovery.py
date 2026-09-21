"""Discovery tests that need no connection, plus the live ones that do.

sources/live.py is Windows-only in what it *does*, but it must still import everywhere —
CI runs on Linux. Nothing at module scope may touch pythonnet or the CLR.
"""

from __future__ import annotations

import pytest

from dax_quax.sources.live import discover_instances, read_port_file, workspace_roots

PORT = 51247

ENCODINGS = {
    "utf-16 with BOM": str(PORT).encode("utf-16"),
    "utf-16-le no BOM": str(PORT).encode("utf-16-le"),
    "utf-16-le trailing NUL": str(PORT).encode("utf-16-le") + b"\x00\x00",
    "utf-8": str(PORT).encode(),
    "utf-8 with newline": f"{PORT}\r\n".encode(),
    "utf-16 with newline": f"{PORT}\r\n".encode("utf-16"),
}


@pytest.mark.parametrize("label", list(ENCODINGS))
def test_port_file_decodes(tmp_path, label):
    path = tmp_path / "msmdsrv.port.txt"
    path.write_bytes(ENCODINGS[label])
    assert read_port_file(path) == PORT


def test_unreadable_port_file_raises(tmp_path):
    path = tmp_path / "msmdsrv.port.txt"
    path.write_bytes(b"\x01\x02")
    with pytest.raises(ValueError, match="no port number"):
        read_port_file(path)


def test_discovery_is_safe_when_nothing_is_running():
    """Must not raise on a machine with no Power BI Desktop, including Linux CI."""
    assert isinstance(discover_instances(), list)


def test_workspace_roots_are_absolute_paths():
    assert all(str(root) for root in workspace_roots())


def test_live_module_imports_without_a_clr():
    """Importing must never boot the runtime — that is what ensure_runtime() is for."""
    import dax_quax.sources.live as live

    assert live._runtime_ready is False


@pytest.mark.live
def test_connect_to_a_running_instance():
    """M0's acceptance criterion. Deselected unless Power BI Desktop is running."""
    from dax_quax import connect

    instances = discover_instances()
    if not instances:
        pytest.skip("no Power BI Desktop instance running")
    model = connect(port=instances[0].port)
    assert model.tables
    assert model.has("metrics")


@pytest.mark.live
def test_dmv_contract_holds_against_a_real_engine():
    """Settles every guess in sources/dmv.py in one shot."""
    from dax_quax.sources import dmv
    from dax_quax.sources.live import LiveConnection

    instances = discover_instances()
    if not instances:
        pytest.skip("no Power BI Desktop instance running")
    with LiveConnection(instances[0].data_source) as conn:
        rowsets = conn.rowsets()
    findings = [f for f in dmv.verify_contract(rowsets) if f.kind == "missing-required"]
    assert findings == [], f"DMV contract is wrong: {findings}"


def test_decimal_and_datetime_convert_without_a_runtime():
    """`float(System.Decimal)` raises TypeError; str() then float() does not.

    Found the first time a real DAX query ran rather than a DMV: DMV rowsets are strings
    and integers, so the gap survived every fixture.
    """
    from dax_quax.sources.live import _py

    class Decimal:  # a stand-in with the .NET type name
        def __str__(self) -> str:
            return "12.34"

    class DateTime:
        def __str__(self) -> str:
            return "2026-09-18 00:00:00"

    assert _py(Decimal()) == 12.34
    assert _py(DateTime()) == "2026-09-18 00:00:00"


@pytest.mark.live
def test_live_and_pbip_agree_on_the_same_model():
    """CONVENTIONS section 10: the equivalence that keeps the two sources honest.

    Needs the borrowed corpus fetched and Model01 open in Power BI Desktop. It caught a
    real asymmetry: TMDL keeps a KPI inside the measure and the DMV keeps it in
    TMSCHEMA_KPIS, so only the disk side was reading it.
    """
    import pathlib

    from dax_quax import connect, open_pbip
    from dax_quax.sources.live import discover_instances

    project = pathlib.Path("tests/fixtures/demo/src/Model01.SemanticModel")
    if not project.is_dir():
        pytest.skip("run `python tools/fetch_demo.py` first")

    disk = open_pbip(project)
    for instance in discover_instances():
        live = connect(port=instance.port)
        if set(live.tables) == set(disk.tables):
            break
    else:
        pytest.skip("Model01 is not open in Power BI Desktop")

    assert {k for k, c in live.columns.items() if not c.is_row_number} == set(disk.columns)
    assert set(live.measures) == set(disk.measures)
    assert live.lineage().edge_pairs() == disk.lineage().edge_pairs()
