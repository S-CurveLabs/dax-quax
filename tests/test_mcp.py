"""The MCP surface.

Two things matter more than the rest here. An agent must not be able to read a verdict
without the caveat that says what it means, and nothing reachable from this server may be
able to change a model.
"""

from __future__ import annotations

import json

import pytest

from dax_quax.mcp.tools import EXPOSED, TOOLS, VERDICT_CAVEAT, Session, call, tool_list
from dax_quax.sources.pbip import open_pbip

PBIP = "tests/fixtures/synthetic_pbip"

#: Import the transport with the SDK made unavailable. Run in a subprocess, because a
#: blocked import here would not survive back into the parent's module cache.
_NO_SDK = '''
import sys

class Block:
    # find_spec, not find_module: the latter was removed in 3.12 and is never consulted,
    # so a finder defining it blocks nothing and the test passes without testing.
    def find_spec(self, name, path=None, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(name)
        return None

sys.meta_path.insert(0, Block())

try:
    import mcp
except ImportError:
    pass
else:
    raise AssertionError("the SDK was not blocked, so this proves nothing")

import dax_quax.mcp.server as server
assert server.SERVER_NAME == "dax-quax"
print("ok")
'''


@pytest.fixture
def session():
    return Session(loader=lambda: open_pbip(PBIP), description=PBIP)


@pytest.fixture
def bare_session(synthetic_model):
    """A model with no report attached, so no verdict is defensible."""
    return Session(loader=lambda: synthetic_model, description="fixture")


def result(session, name, **arguments):
    return call(session, name, arguments)["result"]


# -- the fence -------------------------------------------------------------------------


def test_the_exposed_set_is_exactly_this(session):
    """Adding a tool is two edits and this test. That is the point of `EXPOSED`."""
    assert set(EXPOSED) == {
        "describe_model",
        "search_objects",
        "list_findings",
        "explain_object",
        "trace_lineage",
        "report_issues",
        "rescan",
    }


def test_a_registered_tool_is_not_callable_until_it_is_exposed(session, monkeypatch):
    from dax_quax.mcp import tools

    monkeypatch.setitem(tools.TOOLS, "drop_column", object())
    with pytest.raises(Exception, match="unknown tool"):
        call(session, "drop_column", {})


def test_every_exposed_tool_is_registered():
    assert set(TOOLS) >= set(EXPOSED)


def test_a_session_holds_a_snapshot_and_no_connection(session):
    """The real read-only guarantee: a Model cannot run a query (CONVENTIONS §9)."""
    model = session.ensure()
    assert not hasattr(model, "execute")
    assert not hasattr(model, "connection")
    assert not any(hasattr(session, name) for name in ("connection", "cursor", "execute"))


def test_no_tool_name_suggests_a_write():
    forbidden = ("delete", "drop", "remove_", "write", "apply", "set_", "update", "create")
    assert not [n for n in EXPOSED if any(n.startswith(w) or w in n for w in forbidden)]


# -- the caveat travels with the verdict -----------------------------------------------


def test_a_payload_with_a_verdict_carries_the_caveat(session):
    envelope = call(session, "list_findings", {"limit": 5})
    assert envelope["caveat"] == VERDICT_CAVEAT
    assert envelope["verdicts_are_defensible"] is True


def test_every_tool_that_returns_a_verdict_carries_one(session):
    """Enforced centrally, so this holds for tools nobody thought about."""
    calls = {
        "describe_model": {},
        "search_objects": {"query": "Product"},
        "list_findings": {},
        "explain_object": {"key": "Total Sales"},
        "trace_lineage": {"key": "Total Sales"},
        "report_issues": {},
        "rescan": {},
    }
    for name, arguments in calls.items():
        envelope = call(session, name, arguments)
        text = json.dumps(envelope["result"])
        if '"verdict"' in text:
            assert envelope["caveat"], f"{name} returned a verdict with no caveat"


def test_an_untrustworthy_scope_says_so_instead(bare_session):
    envelope = call(bare_session, "list_findings", {})
    assert envelope["verdicts_are_defensible"] is False
    assert "UNKNOWN rather than REMOVE" in envelope["caveat"]
    assert envelope["scope"] == "no report scanned"


def test_the_caveat_is_not_bolted_onto_answers_with_no_verdict(session):
    envelope = call(session, "trace_lineage", {"key": "Total Sales"})
    assert "caveat" not in envelope


def test_every_answer_says_when_it_was_scanned(session):
    """A held snapshot must be visible as one rather than passing for a fresh read."""
    envelope = call(session, "describe_model", {})
    assert envelope["scanned_at"] == session.ensure().scanned_at.isoformat()
    assert envelope["model"] == "Contoso"
    assert envelope["scope"]


# -- orientation -----------------------------------------------------------------------


def test_describe_model_leads_with_what_it_could_not_see(session):
    payload = result(session, "describe_model")
    assert payload["counts"]["measures"] == 3
    assert payload["blind_spots"]
    assert "report" in payload["capabilities"]


def test_describe_model_names_the_reports(session):
    assert [r["name"] for r in result(session, "describe_model")["reports"]] == ["Contoso.Report"]


# -- search and resolution -------------------------------------------------------------


def test_search_finds_by_substring(session):
    payload = result(session, "search_objects", query="product")
    assert payload["matched"] >= 2
    assert all("roduct" in item["key"] for item in payload["items"])


def test_search_can_be_restricted_to_measures(session):
    payload = result(session, "search_objects", query="", kind="measure")
    assert {item["kind"] for item in payload["items"]} == {"measure"}


def test_a_list_says_what_it_left_out(session):
    payload = result(session, "search_objects", query="", limit=2)
    assert payload["shown"] == 2
    assert payload["matched"] > 2


def test_a_measure_resolves_however_it_is_named(session):
    for spelling in ("Total Sales", "[Total Sales]", "Sales[Total Sales]"):
        payload = result(session, "explain_object", key=spelling)
        assert payload["found"], spelling
        assert payload["key"] == "[Total Sales]"


def test_a_miss_suggests_rather_than_shrugs(session):
    """A bare 'not found' sends an agent guessing, and a guessing agent invents names."""
    payload = result(session, "explain_object", key="Amount")
    assert payload["found"] is False
    assert "Sales[Line Amount]" in payload["did_you_mean"]


# -- the drill-in ----------------------------------------------------------------------


def test_explain_object_carries_what_breaks_if_it_goes(session):
    payload = result(session, "explain_object", key="Sales[Line Amount]")
    assert payload["found"]
    assert payload["dependents"]
    assert payload["verdict"]
    assert payload["blind_spots"]


def test_explain_object_carries_the_dax(session):
    payload = result(session, "explain_object", key="Total Sales")
    assert "SUM" in payload["expression"]["text"]


def test_a_long_expression_is_clipped_and_says_so(session, monkeypatch):
    from dax_quax.mcp import tools

    monkeypatch.setattr(tools, "MAX_EXPRESSION_CHARS", 4)
    payload = result(session, "explain_object", key="Total Sales")
    assert payload["expression"]["truncated_chars"] > 0


def test_explain_object_names_the_visuals_that_bind_it(session):
    payload = result(session, "explain_object", key="Total Sales")
    assert payload["report_bindings"]


def test_an_unmeasured_object_reports_no_size_rather_than_zero(session):
    """A pbip with no cache has no metrics. '0 B' would read as free."""
    payload = result(session, "explain_object", key="Sales[Line Amount]")
    assert "storage" not in payload


# -- lineage ---------------------------------------------------------------------------


def test_trace_lineage_walks_both_ways(session):
    payload = result(session, "trace_lineage", key="Sales[Line Amount]")
    assert payload["dependents"]
    assert "dependencies" in payload


def test_trace_lineage_can_be_limited_to_one_direction(session):
    payload = result(session, "trace_lineage", key="Sales[Line Amount]", direction="down")
    assert "dependencies" not in payload
    assert payload["dependents"]


def test_a_sound_graph_does_not_claim_a_gap(session):
    payload = result(session, "trace_lineage", key="Sales[Line Amount]")
    assert "unresolved_references" not in payload


def test_trace_lineage_admits_a_gap_in_the_graph(session):
    """A reference the extractor could not tie to an object is a missing edge. Say so."""
    from dax_quax.analysis.lineage import UnresolvedRef

    session.ensure()
    ghost = UnresolvedRef(owner="[Total Sales]", text="[Ghost]", reason="no such object")
    session.lineage.unresolved = (ghost,)
    payload = result(session, "trace_lineage", key="Sales[Line Amount]")
    assert "edges are missing" in payload["unresolved_references"]


# -- the report layer ------------------------------------------------------------------


def test_report_issues_counts_breakage_separately(session):
    model = session.ensure()
    del model.columns["Product[Product Name]"]
    payload = result(session, "report_issues")
    assert payload["already_broken"] == 1
    assert payload["items"][0]["already_broken"] is True


# -- the session -----------------------------------------------------------------------


def test_nothing_is_read_until_the_first_call():
    """An MCP client starts its servers at launch; one that blocks on a scan looks broken."""
    session = Session(loader=lambda: pytest.fail("loaded too early"), description="x")
    assert session.model is None
    assert tool_list()


def test_rescan_re_reads_the_source(session):
    result(session, "describe_model")
    before = session.scans
    payload = result(session, "rescan")
    assert payload["scans"] == before + 1


def test_a_failing_load_raises_rather_than_yielding_an_empty_model():
    """An agent handed an empty model concludes the model is empty. That is a false REMOVE."""
    def boom():
        raise RuntimeError("Power BI Desktop is not running")

    session = Session(loader=boom, description="live")
    with pytest.raises(RuntimeError, match="not running"):
        call(session, "describe_model", {})


# -- the advertised surface ------------------------------------------------------------


def test_every_tool_advertises_a_schema_and_a_reason_to_call_it():
    for spec in tool_list():
        assert spec.schema["type"] == "object"
        assert len(spec.summary) > 40, spec.name
        for name in spec.schema.get("required", []):
            assert name in spec.schema["properties"]


def test_the_tool_list_is_stable():
    assert [spec.name for spec in tool_list()] == sorted(EXPOSED)


def test_every_payload_is_json_serialisable(session):
    for name in sorted(EXPOSED):
        arguments = {"query": "a"} if name == "search_objects" else {}
        if name in ("explain_object", "trace_lineage"):
            arguments = {"key": "Total Sales"}
        json.dumps(call(session, name, arguments), default=str)


# -- the transport -----------------------------------------------------------------------


def test_the_server_module_imports_without_the_sdk():
    """CI walks every module with no extras installed; this one must survive that.

    Run in a subprocess with the SDK blocked, because importing it here would only prove
    it imports on a machine that has it -- which is every machine this test runs on.
    """
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-c", _NO_SDK], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "ok" in done.stdout


def test_a_missing_sdk_names_the_install_command(monkeypatch):
    import builtins

    import dax_quax.mcp.server as server

    real = builtins.__import__

    def fail(name, *args, **kwargs):
        if name.startswith("mcp") and not name.startswith("dax_quax"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    with pytest.raises(Exception, match=r"dax-quax\[mcp\]"):
        server.create_server(Session(loader=lambda: None))


def test_the_cli_exposes_the_verb():
    from dax_quax.cli import build_parser

    args = build_parser().parse_args(["mcp", "--pbip", PBIP])
    assert args.command == "mcp"


def test_the_cli_refuses_a_workspace_with_a_pointer(capsys):
    from dax_quax.cli import main

    assert main(["mcp", "--workspace", "tests/fixtures/synthetic_workspace"]) == 1
    assert "one model at a time" in capsys.readouterr().err


def test_the_generated_signature_matches_the_declared_schema():
    """The wrapper is generated so the schema is not written twice. Prove they agree."""
    import inspect

    from dax_quax.mcp.server import tool_function
    from dax_quax.mcp.tools import TOOLS

    for spec in tool_list():
        signature = inspect.signature(tool_function(spec, Session(loader=lambda: None)))
        declared = spec.schema.get("properties", {})
        assert set(signature.parameters) == set(declared), spec.name
        for name in spec.schema.get("required", ()):
            assert signature.parameters[name].default is inspect.Parameter.empty
    assert TOOLS


def test_the_server_advertises_every_tool_as_read_only(session):
    """The fence, said in the protocol's own words so a client can see it too."""
    pytest.importorskip("mcp")
    from dax_quax.mcp.server import create_server

    tools = asyncio_run(create_server(session).list_tools())
    assert {tool.name for tool in tools} == set(EXPOSED)
    assert all(tool.annotations.read_only_hint for tool in tools)
    assert not any(tool.annotations.destructive_hint for tool in tools)


def test_the_server_answers_a_call(session):
    pytest.importorskip("mcp")
    from dax_quax.mcp.server import create_server

    result = asyncio_run(create_server(session).call_tool("describe_model", {}))
    payload = json.loads(_text_of(result))
    assert payload["model"] == "Contoso"
    assert payload["result"]["counts"]["measures"] == 3


def test_a_tool_that_takes_arguments_passes_them_through(session):
    pytest.importorskip("mcp")
    from dax_quax.mcp.server import create_server

    server = create_server(session)
    answer = asyncio_run(server.call_tool("search_objects", {"query": "Sales"}))
    assert json.loads(_text_of(answer))["result"]["matched"] >= 1


def test_an_omitted_optional_argument_does_not_override_the_default(session):
    """The SDK sends None for an argument nobody passed; 25 must still mean 25."""
    pytest.importorskip("mcp")
    from dax_quax.mcp.server import create_server

    result = asyncio_run(create_server(session).call_tool("search_objects", {"query": ""}))
    assert json.loads(_text_of(result))["result"]["shown"] > 2


def test_a_failing_tool_answers_instead_of_breaking_the_transport():
    """An agent can read 'Power BI Desktop is not running' and stop. It cannot read a 500."""
    pytest.importorskip("mcp")
    from dax_quax.mcp.server import create_server

    broken = Session(loader=lambda: 1 / 0, description="broken")
    result = asyncio_run(create_server(broken).call_tool("describe_model", {}))
    assert "ZeroDivisionError" in json.loads(_text_of(result))["error"]


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def _text_of(result):
    """The SDK has moved this shape around between versions; keep the reach in one place."""
    return result.content[0].text


def test_a_source_with_no_metrics_does_not_report_zero_bytes(session):
    """'0 B measured' reads as 'this model is free'. The pbip fixture has no cache."""
    payload = result(session, "describe_model")["summary"]
    assert "measured_bytes" not in payload
    assert "no storage metrics" in payload["sizes"]


def test_a_source_with_metrics_reports_them(model_with_report):
    payload = call(Session(loader=lambda: model_with_report), "describe_model", {})["result"]
    assert payload["summary"]["measured_size"] != "0 B"


def test_a_real_client_can_drive_the_real_command():
    """The only test that exercises what a user actually configures: `dax-quax mcp`.

    Everything above reaches past the transport. This spawns the CLI, speaks the protocol
    to it, and reads a verdict back the way an agent would.
    """
    import sys

    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dax_quax.cli", "mcp", "--pbip", PBIP],
    )

    async def drive():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            answer = await client.call_tool("list_findings", {"verdict": "REMOVE"})
            return tools, json.loads(answer.content[0].text)

    tools, payload = asyncio_run(drive())
    assert {tool.name for tool in tools} == set(EXPOSED)
    assert all(tool.annotations.read_only_hint for tool in tools)
    assert payload["caveat"].startswith("A REMOVE verdict")
    assert [item["key"] for item in payload["result"]["items"]] == ["Sales[Order Number]"]
