"""Draw a Diagram as SVG, with the adjacency it needs to highlight a route.

Highlighting happens in the browser off an embedded adjacency map, not by asking a server,
so a diagram behaves the same in a saved file and in `serve`. Clicking a node walks the
graph both ways and dims everything not on a route through it — which is the question a
model diagram is usually being asked: *what does this touch, and what touches it.*

Layout is the same idea as the lineage view — columns by tier, left to right — but sized
for tens of nodes rather than a handful, so nodes are smaller and a crowded tier wraps into
a second column instead of being truncated. Nothing is hidden without saying so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from dax_quax.render._escape import attr, script_safe_json

if TYPE_CHECKING:
    from dax_quax.analysis.diagram import Diagram

__all__ = ["DIAGRAM_STYLES", "render_diagram"]

NODE_W = 150
NODE_H = 42
TIER_GAP = 64
ROW_GAP = 14
PAD_X = 22
PAD_Y = 52
WRAP_AT = 12  # nodes per column before a tier wraps into a second column
_TITLE_CHAR_W = 6.9
_SUB_CHAR_W = 5.2

#: fill, stroke, accent, subtitle — matched to the report palette.
KIND_STYLES: dict[str, tuple[str, str, str, str]] = {
    "table": ("#1e222a", "#333a46", "#7b8ba3", "#6b7381"),
    "measure": ("#1b2522", "#35534c", "#79d3bf", "#5e8078"),
    "model": ("#1f2330", "#3c4459", "#8fa2c8", "#6b7381"),
    "report": ("#211f2c", "#45405c", "#a394d4", "#6f679a"),
}
EDGE_STYLES: dict[str, tuple[str, float, str]] = {
    "relationship": ("#4a5566", 1.5, ""),
    "expression": ("#3b434f", 1.3, ""),
    "binding": ("#4a4560", 1.3, "4 3"),
    "cross_model": ("#5f6f8f", 1.8, ""),
}

#: Styles for the diagram block. Kept here rather than in _styles.html.j2 because they only
#: matter where a diagram is embedded.
DIAGRAM_STYLES = """
.diagram { border: 1px solid var(--line); border-radius: 10px; background: #171a20;
  padding: 8px; overflow-x: auto; }
.diagram svg { display: block; }
.diagram .dg-node rect { transition: opacity .12s ease; }
.diagram.picked .dg-node { opacity: .18; }
.diagram.picked .dg-edge { opacity: .07; }
.diagram.picked .dg-node.on { opacity: 1; }
.diagram.picked .dg-edge.on { opacity: 1; }
.diagram .dg-node { cursor: pointer; }
.diagram .dg-node.root rect:first-of-type { stroke-width: 2.5; }
.dg-bar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  padding: 8px 4px 10px; font-size: 11.5px; color: var(--faint); }
.dg-bar .key { display: flex; align-items: center; gap: 5px; }
.dg-bar .key i { width: 9px; height: 9px; border-radius: 2px; display: block; }
.dg-bar .hint { margin-left: auto; }
.dg-note { font-size: 11.5px; color: #c9a577; padding: 2px 4px 8px; }
"""


@dataclass(frozen=True, slots=True)
class _Placed:
    node_id: str
    x: float
    y: float
    kind: str
    label: str
    sublabel: str
    detail: str
    muted: bool


def _clip(text: str, width: float, char_w: float) -> str:
    limit = int((width - 22) / char_w)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def render_diagram(diagram: Diagram, *, element_id: str = "diagram") -> str:
    """Return the diagram as a self-contained block: SVG, legend and its behaviour."""
    if diagram.is_empty:
        return '<div class="empty"><div class="head">Nothing to draw.</div></div>'

    columns = _columns(diagram)
    placed = _place(diagram, columns)
    width = max((node.x for node in placed.values()), default=0) + NODE_W + PAD_X
    height = max((node.y for node in placed.values()), default=0) + NODE_H + PAD_Y

    parts = [
        f'<div class="diagram" id="{attr(element_id)}">',
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" '
        f'height="{height:.0f}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Diagram of {attr(diagram.title)}">',
        "<defs>"
        '<marker id="dg-a" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">'
        '<path d="M0 0 L6 3 L0 6 z" fill="#5a6475"></path></marker>'
        "</defs>",
    ]

    for tier, label in sorted(diagram.tier_labels.items()):
        if not label:
            continue
        xs = [placed[n].x for n, t in diagram.tiers.items() if t == tier and n in placed]
        if xs:
            parts.append(
                f'<text x="{min(xs):.0f}" y="30" font-family="ui-monospace, monospace" '
                f'font-size="10" letter-spacing="1" fill="#535b68">{escape(label)}</text>'
            )

    for edge in diagram.edges:
        a, b = placed.get(edge.source), placed.get(edge.target)
        if a is None or b is None:
            continue
        stroke, weight, dash = EDGE_STYLES.get(edge.kind, EDGE_STYLES["expression"])
        forward = b.x >= a.x + NODE_W
        x1, y1 = (a.x + NODE_W, a.y + NODE_H / 2) if forward else (a.x, a.y + NODE_H / 2)
        x2, y2 = (b.x, b.y + NODE_H / 2) if forward else (b.x + NODE_W, b.y + NODE_H / 2)
        grip = max(20, abs(x2 - x1) / 2)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<path class="dg-edge" data-a="{attr(edge.source)}" '
            f'data-b="{attr(edge.target)}" '
            f'd="M{x1:.0f} {y1:.0f} C{x1 + grip:.0f} {y1:.0f}, {x2 - grip:.0f} {y2:.0f}, '
            f'{x2:.0f} {y2:.0f}" fill="none" stroke="{stroke}" stroke-width="{weight}"'
            f'{dash_attr} marker-end="url(#dg-a)"></path>'
        )

    for node in placed.values():
        fill, stroke, accent, subtle = KIND_STYLES.get(node.kind, KIND_STYLES["table"])
        opacity = ' opacity="0.55"' if node.muted else ""
        title = escape(f"{node.label} — {node.detail}" if node.detail else node.label)
        parts.append(
            f'<g class="dg-node" data-id="{attr(node.node_id)}"{opacity}>'
            f"<title>{title}</title>"
            f'<rect x="{node.x:.0f}" y="{node.y:.0f}" width="{NODE_W}" height="{NODE_H}" '
            f'rx="6" fill="{fill}" stroke="{stroke}"></rect>'
            f'<rect x="{node.x:.0f}" y="{node.y:.0f}" width="3" height="{NODE_H}" rx="1.5" '
            f'fill="{accent}"></rect>'
            f'<text x="{node.x + 12:.0f}" y="{node.y + 18:.0f}" '
            f'font-family="ui-monospace, monospace" font-size="11.5" fill="#cfd4dc">'
            f"{escape(_clip(node.label, NODE_W, _TITLE_CHAR_W))}</text>"
            f'<text x="{node.x + 12:.0f}" y="{node.y + 32:.0f}" '
            f'font-family="ui-sans-serif, system-ui, sans-serif" font-size="9.5" '
            f'fill="{subtle}">{escape(_clip(node.sublabel, NODE_W, _SUB_CHAR_W))}</text>'
            f"</g>"
        )

    parts.append("</svg></div>")

    for note in diagram.notes:
        parts.append(f'<div class="dg-note">{escape(note)}</div>')

    kinds = {node.kind for node in diagram.nodes}
    legend = "".join(
        f'<span class="key"><i style="background:{KIND_STYLES[kind][2]}"></i>{kind}</span>'
        for kind in ("model", "report", "table", "measure")
        if kind in kinds
    )
    parts.append(
        f'<div class="dg-bar">{legend}'
        '<span class="hint">click a node to highlight every route through it; '
        "click it again to clear</span></div>"
    )
    parts.append(_behaviour(element_id, diagram))
    return "".join(parts)


def _behaviour(element_id: str, diagram: Diagram) -> str:
    adjacency = script_safe_json(diagram.neighbours())
    return f"""<script>
(function () {{
  const box = document.getElementById({json.dumps(element_id)});
  if (!box) return;
  const near = {adjacency};
  let picked = null;
  function reach(start) {{
    const seen = new Set([start]);
    const queue = [start];
    while (queue.length) {{
      for (const next of near[queue.pop()] || []) {{
        if (!seen.has(next)) {{ seen.add(next); queue.push(next); }}
      }}
    }}
    return seen;
  }}
  function paint() {{
    const on = picked ? reach(picked) : null;
    box.classList.toggle("picked", !!picked);
    box.querySelectorAll(".dg-node").forEach(n => {{
      n.classList.toggle("on", !!on && on.has(n.dataset.id));
      n.classList.toggle("root", n.dataset.id === picked);
    }});
    box.querySelectorAll(".dg-edge").forEach(e => {{
      e.classList.toggle("on", !!on && on.has(e.dataset.a) && on.has(e.dataset.b));
    }});
  }}
  box.addEventListener("click", (event) => {{
    const node = event.target.closest(".dg-node");
    picked = (!node || node.dataset.id === picked) ? null : node.dataset.id;
    paint();
  }});
}})();
</script>"""


def _columns(diagram: Diagram) -> list[tuple[int, list[str]]]:
    """One entry per drawn column. A tier with too many nodes wraps into several."""
    grouped: dict[int, list[str]] = {}
    for node in diagram.nodes:
        grouped.setdefault(diagram.tiers.get(node.id, 0), []).append(node.id)

    columns: list[tuple[int, list[str]]] = []
    for tier in sorted(grouped):
        members = sorted(grouped[tier])
        for start in range(0, len(members), WRAP_AT):
            columns.append((tier, members[start : start + WRAP_AT]))
    return columns


def _place(diagram: Diagram, columns: list[tuple[int, list[str]]]) -> dict[str, _Placed]:
    by_id = {node.id: node for node in diagram.nodes}
    tallest = max((len(members) for _, members in columns), default=1)
    block_height = tallest * NODE_H + (tallest - 1) * ROW_GAP

    placed: dict[str, _Placed] = {}
    for index, (_, members) in enumerate(columns):
        x = PAD_X + index * (NODE_W + TIER_GAP)
        own = len(members) * NODE_H + (len(members) - 1) * ROW_GAP
        top = PAD_Y + (block_height - own) / 2
        for position, node_id in enumerate(members):
            node = by_id[node_id]
            placed[node_id] = _Placed(
                node_id=node_id,
                x=x,
                y=top + position * (NODE_H + ROW_GAP),
                kind=node.kind,
                label=node.label,
                sublabel=node.sublabel,
                detail=node.detail,
                muted=node.muted,
            )
    return placed
