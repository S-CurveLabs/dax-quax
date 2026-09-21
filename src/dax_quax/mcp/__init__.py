"""An MCP server over the read-only surface: scan, lineage, findings.

`tools.py` is the surface and needs nothing beyond the library. `server.py` is the stdio
transport and needs the [mcp] extra. Read `tools.py` first: it says why the read-only
fence is where it is, and why an agent is handed the scope caveat whether it asks or not.
"""

from __future__ import annotations

from dax_quax.mcp.tools import EXPOSED, TOOLS, Session, ToolSpec, call, tool_list

__all__ = ["EXPOSED", "TOOLS", "Session", "ToolSpec", "call", "tool_list"]
