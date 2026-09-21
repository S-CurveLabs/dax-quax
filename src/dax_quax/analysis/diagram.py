"""Diagrams at the altitude a person can actually read.

The lineage graph is object-level: every column, every measure. On a real model that is a
few thousand nodes and unreadable as a picture, which is why every lineage view is scoped
to a focus and a radius. A *diagram* is the other answer — roll the same facts up until the
whole thing fits on a screen:

    workspace   models, the reports over them, and the models that read other models
    model       tables, the relationships between them, and the measures they feed

Both are the same structure, so both render and behave identically.

Layering is a longest-path topological pass, so dependencies read left to right: a
dimension filters into a fact, a fact feeds a measure. A cycle (two tables related in both
directions, say) cannot be layered, so the nodes involved are placed at the depth reached
before the cycle closed, and the fact is recorded rather than hidden.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from dax_quax.dax.extract import extract
from dax_quax.dax.resolve import ModelIndex, resolve
from dax_quax.model import Model

if TYPE_CHECKING:
    from dax_quax.sources.workspace import Workspace

__all__ = [
    "Diagram",
    "DiagramEdge",
    "DiagramNode",
    "layer",
    "model_diagram",
    "workspace_diagram",
]

NodeKind = Literal["model", "report", "table", "measure"]
EdgeKind = Literal["relationship", "expression", "binding", "cross_model"]


@dataclass(frozen=True, slots=True)
class DiagramNode:
    id: str
    label: str
    kind: NodeKind
    sublabel: str = ""
    detail: str = ""
    muted: bool = False


@dataclass(frozen=True, slots=True)
class DiagramEdge:
    source: str
    target: str
    kind: EdgeKind
    label: str = ""


@dataclass(slots=True)
class Diagram:
    title: str
    nodes: list[DiagramNode] = field(default_factory=list)
    edges: list[DiagramEdge] = field(default_factory=list)
    tiers: dict[str, int] = field(default_factory=dict)
    tier_labels: dict[int, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.nodes

    def neighbours(self) -> dict[str, list[str]]:
        """Adjacency in both directions, for highlighting a route through a node."""
        both: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            both[edge.source].append(edge.target)
            both[edge.target].append(edge.source)
        return dict(both)


def layer(node_ids: list[str], edges: list[DiagramEdge]) -> tuple[dict[str, int], list[str]]:
    """Longest-path layering. Returns the tier of each node, and any nodes left in a cycle.

    A node sits one tier right of its deepest predecessor, so every edge points rightward
    unless a cycle prevents it.
    """
    incoming: dict[str, list[str]] = {node: [] for node in node_ids}
    outgoing: dict[str, list[str]] = {node: [] for node in node_ids}
    for edge in edges:
        if edge.source in incoming and edge.target in incoming:
            incoming[edge.target].append(edge.source)
            outgoing[edge.source].append(edge.target)

    remaining = {node: len(set(sources)) for node, sources in incoming.items()}
    tiers = {node: 0 for node in node_ids}
    frontier = [node for node, count in remaining.items() if count == 0]
    settled: set[str] = set()

    while frontier:
        node = frontier.pop()
        settled.add(node)
        for target in set(outgoing[node]):
            tiers[target] = max(tiers[target], tiers[node] + 1)
            remaining[target] -= 1
            if remaining[target] == 0:
                frontier.append(target)

    cycled = [node for node in node_ids if node not in settled]
    return tiers, cycled


# -- one model ------------------------------------------------------------------------------


def model_diagram(model: Model, *, include_measures: bool = True) -> Diagram:
    """Tables, their relationships, and the measures they feed."""
    diagram = Diagram(title=model.name)
    metrics = model.has("metrics")

    for table in model.tables.values():
        columns = [c for c in model.columns_of(table.name) if not c.is_row_number]
        measured = [c for c in columns if c.metrics]
        size = sum(c.metrics.total_bytes for c in measured) if measured else None
        detail = f"{len(columns)} column(s)"
        if table.row_count:
            detail += f", {table.row_count:,} row(s)"
        diagram.nodes.append(
            DiagramNode(
                id=table.name,
                label=table.name,
                kind="table",
                sublabel=_bytes(size) if metrics and size else (table.mode or "table"),
                detail=detail,
                muted=table.is_auto_date_time,
            )
        )

    for relationship in model.relationships:
        # Drawn in the direction filters travel: the one side into the many side, which is
        # how people read a star schema, rather than the direction the metadata records.
        diagram.edges.append(
            DiagramEdge(
                source=relationship.to_table,
                target=relationship.from_table,
                kind="relationship",
                label="" if relationship.is_active else "inactive",
            )
        )

    if include_measures:
        index = ModelIndex(model)
        for measure in model.measures.values():
            diagram.nodes.append(
                DiagramNode(
                    id=measure.key,
                    label=measure.name,
                    kind="measure",
                    sublabel=measure.table,
                    detail=(measure.expression or "").strip()[:160],
                    muted=measure.is_hidden,
                )
            )
        for measure in model.measures.values():
            for source in _feeders(measure, index, model):
                if source != measure.key:
                    diagram.edges.append(
                        DiagramEdge(source=source, target=measure.key, kind="expression")
                    )

    _finish(diagram, {"table": "TABLES", "measure": "MEASURES"}, floors={"measure": 1})
    return diagram


def _feeders(measure, index: ModelIndex, model: Model) -> set[str]:  # noqa: ANN001
    """The tables and measures one measure reads, rolled up from its column references."""
    resolution = resolve(extract(measure.expression), index, measure.table)
    feeders: set[str] = set()
    for reference in resolution.references:
        ref = reference.ref
        if ref.kind == "measure":
            feeders.add(ref.key)
        elif ref.kind == "table" and ref.name in model.tables:
            feeders.add(ref.name)
        elif ref.kind == "column" and ref.table in model.tables:
            feeders.add(ref.table)
    return feeders


# -- a whole workspace -----------------------------------------------------------------------


def workspace_diagram(workspace: Workspace) -> Diagram:
    """Models, the reports over them, and the models that read other models."""
    diagram = Diagram(title=workspace.root.name)
    orphans = set(workspace.orphan_models())

    for name, model in workspace.models.items():
        reports = workspace.reports_for(name)
        diagram.nodes.append(
            DiagramNode(
                id=f"model:{name}",
                label=name,
                kind="model",
                sublabel=f"{len(model.tables)} table(s), {len(model.measures)} measure(s)",
                detail=(
                    "no report in this scan references it"
                    if name in orphans
                    else f"{len(reports)} report(s)"
                ),
                muted=name in orphans,
            )
        )

    for link in workspace.links:
        diagram.nodes.append(
            DiagramNode(
                id=f"report:{link.name}",
                label=link.name,
                kind="report",
                sublabel="unmatched" if not link.matched else (link.model or ""),
                detail=link.reason or "",
                muted=not link.matched,
            )
        )
        if link.matched:
            diagram.edges.append(
                DiagramEdge(
                    source=f"model:{link.model}",
                    target=f"report:{link.name}",
                    kind="binding",
                )
            )

    for reader, target in workspace.cross_model:
        if target and f"model:{target}" in {node.id for node in diagram.nodes}:
            diagram.edges.append(
                DiagramEdge(
                    source=f"model:{target}", target=f"model:{reader}", kind="cross_model"
                )
            )

    if workspace.unmatched:
        diagram.notes.append(
            f"{len(workspace.unmatched)} report(s) float free: they name a dataset that is "
            "not in this scan, so nothing can be said about what they use"
        )

    # A report with no edges would otherwise land in the models column, which reads as
    # though it were one.
    _finish(diagram, {"model": "MODELS", "report": "REPORTS"}, floors={"report": 1})
    return diagram


# -- shared finishing ---------------------------------------------------------------------------


def _finish(
    diagram: Diagram, labels: dict[str, str], *, floors: dict[str, int] | None = None
) -> None:
    ids = [node.id for node in diagram.nodes]
    tiers, cycled = layer(ids, diagram.edges)
    kinds_by_id = {node.id: node.kind for node in diagram.nodes}
    for kind, floor in (floors or {}).items():
        for node_id, kind_ in kinds_by_id.items():
            if kind_ == kind:
                tiers[node_id] = max(tiers[node_id], floor)
    diagram.tiers = tiers

    if cycled:
        diagram.notes.append(
            f"{len(cycled)} node(s) sit in a cycle, so their left-to-right position is "
            "arbitrary: " + ", ".join(sorted(cycled)[:4]) + ("…" if len(cycled) > 4 else "")
        )

    by_tier: dict[int, set[str]] = defaultdict(set)
    for node_id, tier in tiers.items():
        by_tier[tier].add(kinds_by_id[node_id])
    for tier, present in by_tier.items():
        named = [labels[kind] for kind in present if kind in labels]
        diagram.tier_labels[tier] = named[0] if len(named) == 1 else ""


def _bytes(size: int | None) -> str:
    if not size:
        return "table"
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"
