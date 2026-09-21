"""The MCP transport. Everything interesting is in `tools.py`.

The SDK is imported inside the functions rather than at module scope, so this file still
imports with nothing optional installed — which is what the `imports-without-extras` CI
job checks for every module but `sources/live.py`. The module importing and the server
starting are different claims, and only the second one needs the dependency.

This package is called `mcp` and so is the SDK. Absolute imports mean `import mcp.types`
here reaches the SDK, not this package; the names are worth the moment of doubt because
`dax_quax.mcp` is where anyone would look for it.

WHY A WRAPPER IS GENERATED
--------------------------
The SDK builds a tool's JSON schema by introspecting the function it is handed. Our tools
declare their schema outright, in a module that has no pydantic and no SDK to declare it
with. Rather than write each schema twice and let the two drift, the declared schema is
turned into a signature the SDK can read. The declaration stays the single source of truth
and `tests/test_mcp.py` checks the generated signature against it.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any

from dax_quax.errors import DaxQuaxError
from dax_quax.mcp.tools import Session, call, tool_list

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from dax_quax.mcp.tools import ToolSpec
    from dax_quax.model import Model

__all__ = ["create_server", "serve_stdio", "session_for", "tool_function"]

SERVER_NAME = "dax-quax"

_MISSING = (
    "the MCP server needs the Python MCP SDK, which is not installed. "
    "Install it with: pip install 'dax-quax[mcp]'"
)

#: JSON schema types, as the annotations the SDK will introspect.
_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def session_for(loader: Callable[[], Model], description: str, thresholds: Any = None) -> Session:
    """A session that has not read anything yet.

    Loading is deferred to the first call on purpose: an MCP client starts its servers
    when it starts, and a server that spends ten seconds reading a model before it will
    answer `list_tools` looks broken.
    """
    return Session(loader=loader, description=description, thresholds=thresholds)


def tool_function(spec: ToolSpec, session: Session) -> Callable[..., str]:
    """One tool, as a function the SDK can introspect into `spec.schema`."""
    properties: dict[str, Any] = spec.schema.get("properties", {})
    required = set(spec.schema.get("required", ()))

    parameters, annotations = [], {}
    for name, field in properties.items():
        kind = _TYPES.get(field.get("type", "string"), str)
        if name in required:
            annotations[name] = kind
            default = inspect.Parameter.empty
        else:
            annotations[name] = kind | None
            default = field.get("default")
        parameters.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=kind
            )
        )

    def run(**arguments: Any) -> str:
        # An omitted optional argument arrives as None; the handlers have their own
        # defaults and should not be told None means "use 25".
        given = {k: v for k, v in arguments.items() if v is not None}
        try:
            payload: dict[str, Any] = call(session, spec.name, given)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
            # An agent can read "Power BI Desktop is not running" and stop. It cannot read
            # a transport error, and will try the same call again.
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        return json.dumps(payload, indent=2, default=str)

    run.__name__ = spec.name
    run.__doc__ = spec.summary
    run.__signature__ = inspect.Signature(parameters, return_annotation=str)
    run.__annotations__ = {**annotations, "return": str}
    return run


def create_server(session: Session) -> Any:
    """Wire the tool registry onto an MCP server. No behaviour lives here."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise DaxQuaxError(_MISSING) from exc

    server = MCPServer(SERVER_NAME)
    for spec in tool_list():
        server.add_tool(
            tool_function(spec, session),
            name=spec.name,
            description=spec.summary,
            # The fence, said in the protocol's own words so a client can see it too.
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, openWorldHint=False
            ),
        )
    return server


def serve_stdio(session: Session) -> None:
    """Run the server over stdio until the client closes it."""
    create_server(session).run(transport="stdio")
