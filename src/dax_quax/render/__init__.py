"""Rendering. One template set, two delivery modes: a file (M5) and HTTP (M6).

Byte formatting lives here and nowhere else — everything upstream carries ints.
"""

from dax_quax.render.report import build_context, format_bytes, render_report

__all__ = ["build_context", "format_bytes", "render_report"]
