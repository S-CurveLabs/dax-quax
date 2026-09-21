"""Draw a lineage neighbourhood as SVG, laid out in tiers by hop distance.

WHY NOT A GRAPH LIBRARY
-----------------------
A focus-plus-radius view is a handful of nodes in columns ordered by hop distance. That
layout is a sort and some arithmetic, so a 1 MB JavaScript graph engine would buy pan and
zoom at the cost of a dependency, a vendoring step, and a picture that only exists in the
browser. Server-rendered SVG works the same in the served app, in a saved page and in a
screenshot pasted into a ticket.

The layout is deliberately tiered rather than force-directed: dependencies read left to
right, which is the direction people actually ask the question in — "what feeds this" on
the left, "what breaks if I drop it" on the right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from xml.sax.saxutils import escape

from dax_quax.render._escape import attr

if TYPE_CHECKING:
    from dax_quax.analysis.lineage import LineageGraph

__all__ = ["NODE_STYLES", "render_lineage_svg", "tiered_layout"]

Direction = Literal["up", "down", "both"]

NODE_W = 158
NODE_H = 48
FOCUS_H = 58
TIER_GAP = 52
ROW_GAP = 26
PAD_X = 20
PAD_Y = 74
MAX_PER_TIER = 8
#: Average advance per character, used to decide where to truncate. The two label
#: lines use different faces and sizes, so one figure for both clips the subtitle
#: roughly a third early ("Sales - calc colu...").
_TITLE_CHAR_W = 7.3   # 12.5px monospace
_SUBTITLE_CHAR_W = 5.4  # 10.5px sans

#: fill, stroke, accent, subtitle colour — matched to the report's palette.
NODE_STYLES: dict[str, tuple[str, str, str, str]] = {
    "column": ("#1e222a", "#333a46", "#7b8ba3", "#6b7381"),
    "calc_column": ("#241f18", "#574733", "#e3a85e", "#8a7455"),
    "measure": ("#1b2522", "#35534c", "#79d3bf", "#5e8078"),
    "table": ("#1c1f26", "#3a4150", "#9aa1ae", "#6b7381"),
    "relationship": ("#211f2c", "#45405c", "#a394d4", "#6f679a"),
}
_FOCUS = ("#22403a", "#79d3bf", "#79d3bf", "#7fb5aa")


@dataclass(frozen=True, slots=True)
class Placed:
    key: str
    tier: int
    x: float
    y: float
    w: float
    h: float
    kind: str
    title: str
    subtitle: str
    is_focus: bool

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def middle(self) -> float:
        return self.y + self.h / 2


@dataclass(slots=True)
class Layout:
    nodes: dict[str, Placed]
    edges: list[tuple[str, str]]
    width: float
    height: float
    tiers: dict[int, float]
    truncated: int = 0


def tiered_layout(
    lineage: LineageGraph,
    focus: str,
    radius: int = 2,
    direction: Direction = "both",
) -> Layout:
    """Place the neighbourhood of ``focus`` into columns by hop distance."""
    graph = lineage.graph
    if focus not in graph:
        return Layout(nodes={}, edges=[], width=0, height=0, tiers={})

    depth: dict[str, int] = {focus: 0}
    if direction in ("up", "both"):
        _walk(graph.predecessors, focus, radius, -1, depth)
    if direction in ("down", "both"):
        _walk(graph.successors, focus, radius, 1, depth)

    by_tier: dict[int, list[str]] = {}
    for key, level in depth.items():
        by_tier.setdefault(level, []).append(key)

    truncated = 0
    for level, keys in by_tier.items():
        keys.sort()
        if len(keys) > MAX_PER_TIER:
            truncated += len(keys) - MAX_PER_TIER
            by_tier[level] = keys[:MAX_PER_TIER]

    kept = {key for keys in by_tier.values() for key in keys}
    levels = sorted(by_tier)
    tallest = max(
        (len(keys) * NODE_H + (len(keys) - 1) * ROW_GAP for keys in by_tier.values()),
        default=NODE_H,
    )
    height = PAD_Y + tallest + PAD_Y / 2
    width = PAD_X * 2 + len(levels) * NODE_W + (len(levels) - 1) * TIER_GAP

    placed: dict[str, Placed] = {}
    tiers: dict[int, float] = {}
    for index, level in enumerate(levels):
        x = PAD_X + index * (NODE_W + TIER_GAP)
        tiers[level] = x + NODE_W / 2
        keys = by_tier[level]
        block = len(keys) * NODE_H + (len(keys) - 1) * ROW_GAP
        top = PAD_Y + (tallest - block) / 2
        for position, key in enumerate(keys):
            data = graph.nodes[key]
            is_focus = key == focus
            placed[key] = Placed(
                key=key,
                tier=level,
                x=x,
                y=top + position * (NODE_H + ROW_GAP) - (FOCUS_H - NODE_H) / 2 * is_focus,
                w=NODE_W,
                h=FOCUS_H if is_focus else NODE_H,
                kind=data.get("kind", "column"),
                title=str(data.get("name", key)),
                subtitle=_subtitle(data),
                is_focus=is_focus,
            )

    edges = [(u, v) for u, v in graph.edges() if u in kept and v in kept]
    return Layout(placed, edges, width, height, tiers, truncated)


def _walk(step, start: str, radius: int, sign: int, depth: dict[str, int]) -> None:
    frontier = [start]
    for hop in range(1, radius + 1):
        following: list[str] = []
        for node in frontier:
            for neighbour in step(node):
                if neighbour not in depth:
                    depth[neighbour] = sign * hop
                    following.append(neighbour)
        frontier = following
        if not frontier:
            break


def _subtitle(data: dict) -> str:
    kind = data.get("kind", "")
    if kind == "relationship":
        return "relationship"
    if kind == "measure":
        return f"{data.get('table') or ''} · measure".strip(" ·")
    if kind == "calc_column":
        return f"{data.get('table') or ''} · calc column".strip(" ·")
    if kind == "table":
        return "table"
    return str(data.get("table") or "")


def _clip(text: str, width: float, char_w: float = _TITLE_CHAR_W) -> str:
    limit = int((width - 26) / char_w)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def render_lineage_svg(
    lineage: LineageGraph,
    focus: str,
    radius: int = 2,
    direction: Direction = "both",
) -> str:
    """Return a standalone <svg> for the neighbourhood of ``focus``."""
    layout = tiered_layout(lineage, focus, radius, direction)
    if not layout.nodes:
        return (
            '<div class="empty"><div class="head">Nothing to draw: this object has no '
            "lineage in the scanned model.</div></div>"
        )

    parts: list[str] = [
        f'<svg viewBox="0 0 {layout.width:.0f} {layout.height:.0f}" '
        f'style="display:block;width:100%;height:auto" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Lineage around {attr(focus)}">',
        '<defs>'
        '<marker id="a-dim" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">'
        '<path d="M0 0 L6 3 L0 6 z" fill="#3b434f"></path></marker>'
        '<marker id="a-lit" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">'
        '<path d="M0 0 L6 3 L0 6 z" fill="#6aa89c"></path></marker>'
        "</defs>",
    ]

    for level, centre in layout.tiers.items():
        label = "FOCUS" if level == 0 else f"{level:+d} HOP" + ("S" if abs(level) != 1 else "")
        colour = "#79d3bf" if level == 0 else "#535b68"
        parts.append(
            f'<text x="{centre:.0f}" y="46" text-anchor="middle" '
            f'font-family="ui-monospace, monospace" font-size="10" letter-spacing="1" '
            f'fill="{colour}">{label}</text>'
        )

    for source, target in layout.edges:
        a, b = layout.nodes[source], layout.nodes[target]
        lit = a.is_focus or b.is_focus
        stroke, marker, width = (
            ("#6aa89c", "a-lit", 2) if lit else ("#3b434f", "a-dim", 1.4)
        )
        # Left-to-right when the tiers say so; otherwise a same-tier or backward edge,
        # which is drawn from whichever side is nearer so the arrow still reads.
        x1, y1 = (a.right, a.middle) if b.x >= a.right else (a.x, a.middle)
        x2, y2 = (b.x, b.middle) if b.x >= a.right else (b.right, b.middle)
        grip = max(18, abs(x2 - x1) / 2)
        parts.append(
            f'<path d="M{x1:.0f} {y1:.0f} C{x1 + grip:.0f} {y1:.0f}, '
            f'{x2 - grip:.0f} {y2:.0f}, {x2:.0f} {y2:.0f}" fill="none" '
            f'stroke="{stroke}" stroke-width="{width}" marker-end="url(#{marker})"></path>'
        )

    for node in layout.nodes.values():
        fill, stroke, accent, subtle = (
            _FOCUS if node.is_focus else NODE_STYLES.get(node.kind, NODE_STYLES["column"])
        )
        dash = ' stroke-dasharray="4 3"' if node.kind == "relationship" else ""
        if node.is_focus:
            parts.append(
                f'<rect x="{node.x - 8:.0f}" y="{node.y - 8:.0f}" width="{node.w + 16:.0f}" '
                f'height="{node.h + 16:.0f}" rx="10" fill="none" stroke="#79d3bf" '
                f'stroke-width="1" opacity="0.28"></rect>'
            )
        parts.append(
            f'<g><rect x="{node.x:.0f}" y="{node.y:.0f}" width="{node.w:.0f}" '
            f'height="{node.h:.0f}" rx="7" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{2 if node.is_focus else 1}"{dash}></rect>'
            f'<rect x="{node.x:.0f}" y="{node.y:.0f}" width="3" height="{node.h:.0f}" '
            f'rx="1.5" fill="{accent}"></rect>'
            f'<text x="{node.x + 14:.0f}" y="{node.y + 19:.0f}" '
            f'font-family="ui-sans-serif, system-ui, sans-serif" font-size="10.5" '
            f'fill="{subtle}">{escape(_clip(node.subtitle, node.w, _SUBTITLE_CHAR_W))}</text>'
            f'<text x="{node.x + 14:.0f}" y="{node.y + (38 if node.is_focus else 36):.0f}" '
            f'font-family="ui-monospace, monospace" '
            f'font-size="{13.5 if node.is_focus else 12.5}" '
            f'fill="{"#ffffff" if node.is_focus else "#cfd4dc"}">'
            f"{escape(_clip(node.title, node.w))}</text></g>"
        )

    if layout.truncated:
        parts.append(
            f'<text x="{PAD_X}" y="{layout.height - 14:.0f}" '
            f'font-family="ui-monospace, monospace" font-size="10.5" fill="#e3a85e">'
            f"{layout.truncated} further node(s) not drawn &#8212; narrow the radius"
            f"</text>"
        )

    parts.append("</svg>")
    return "".join(parts)
