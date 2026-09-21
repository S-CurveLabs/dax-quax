"""Escaping for the two contexts the renderers embed untrusted names in.

There were two partial copies of the JSON one: report.py escaped three characters,
diagram.py escaped two. The one that missed '>' was inert in practice but meant the rule
lived in two places and drifted, which is how the next copy ends up missing '<'.

The attribute one exists because ``xml.sax.saxutils.escape`` does **not** escape the quote
characters, and every SVG this package writes puts a model object's name inside a
double-quoted attribute. A table called ``a" onload="alert(1)`` closes the attribute and
opens an event handler on a page the reader opened to decide what to delete.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["attr", "script_safe_json"]

#: Characters that would let embedded JSON escape its <script> element, mapped to the JSON
#: escape sequence that renders each one inert.
_SCRIPT_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
)


#: Everything that can break out of a quoted XML/HTML attribute value. Both quote styles
#: are escaped so the result is safe in either, and '&' is first so it cannot double-escape.
_ATTR_ESCAPES = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&#39;"),
)


def attr(text: str) -> str:
    """Text safe inside a quoted attribute value. The caller supplies the quotes."""
    for char, escaped in _ATTR_ESCAPES:
        text = text.replace(char, escaped)
    return text


def script_safe_json(payload: Any) -> str:
    """JSON that cannot break out of the <script> block it is embedded in.

    json.dumps leaves '<' alone, so an object named "</script>" would close the element and
    everything after it would be parsed as HTML.
    """
    text = json.dumps(payload)
    for char, escape in _SCRIPT_ESCAPES:
        text = text.replace(char, escape)
    return text
