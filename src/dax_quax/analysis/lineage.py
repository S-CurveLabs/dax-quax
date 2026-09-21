"""The lineage graph: what depends on what.

Edges point **from a dependency to its dependent**, so following successors answers "what
breaks if I delete this" and following predecessors answers "what does this need". That
direction also matches how the graph is drawn — sources on the left, consumers on the right.

    Sales[Quantity] ──expression──▶ [Total Sales] ──expression──▶ [Margin %]

Relationships get their own node rather than an edge between the two columns, which would
make a two-cycle and break traversal. A relationship genuinely *depends on* both of its
endpoint columns, so ``Sales[Product Key] ──▶ (relationship) ◀── Product[Product Key]``
states the truth and keeps the graph acyclic in the usual case.

WHAT THIS GRAPH DOES NOT KNOW
-----------------------------
Only the expression sources present on the Model are walked. Anything the loader does not
yet populate is missing, and a missing edge becomes a false "unused" verdict later — so the
omissions are listed in :attr:`LineageGraph.gaps` rather than left implicit. Check that
attribute before trusting a zero-degree node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import networkx as nx

from dax_quax.dax.extract import extract
from dax_quax.dax.resolve import ModelIndex, Resolution
from dax_quax.model import Model

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "EdgeKind",
    "LineageGraph",
    "UnresolvedRef",
    "build_lineage",
    "calc_dependency_pairs",
    "diff_against_engine",
]

EdgeKind = Literal["expression", "relationship", "sort_by", "group_by", "hierarchy"]

Direction = Literal["up", "down", "both"]

#: Expression sources the Model cannot yet carry. Each entry is a gap in the graph, and
#: each will be deleted as the milestone that loads it lands.
_STRUCTURAL_GAPS = (
    ("perspectives", "an object exposed only through a perspective is not tracked; this "
     "cannot cause a false REMOVE, since a perspective only hides things"),
    ("translations and cultures", "a caption or description in another culture is not "
     "walked for references"),
)


@dataclass(frozen=True, slots=True)
class UnresolvedRef:
    """A reference that could not be tied to an object, and whose expression it was in."""

    owner: str
    text: str
    reason: str


@dataclass(slots=True)
class LineageGraph:
    graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    unresolved: tuple[UnresolvedRef, ...] = ()
    notes: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    # -- traversal -----------------------------------------------------------------------

    def dependencies(self, key: str, hops: int | None = None) -> tuple[str, ...]:
        """What ``key`` needs. Predecessors, transitively unless ``hops`` limits it."""
        return self._walk(key, self.graph.predecessors, hops)

    def dependents(self, key: str, hops: int | None = None) -> tuple[str, ...]:
        """What needs ``key`` — i.e. what breaks if it is deleted."""
        return self._walk(key, self.graph.successors, hops)

    #: Aliases used by the UI, where the graph reads left to right.
    upstream = dependencies
    downstream = dependents

    def is_referenced(self, key: str) -> bool:
        """Whether anything in the model depends on this object.

        False does **not** mean unused — see :attr:`gaps` and the report layer.
        """
        return key in self.graph and self.graph.out_degree(key) > 0

    def reference_count(self, key: str) -> int:
        return self.graph.out_degree(key) if key in self.graph else 0

    def neighborhood(self, key: str, radius: int = 2, direction: Direction = "both") -> nx.DiGraph:
        """The subgraph within ``radius`` hops of ``key``.

        Always use this for a visual. A whole-model lineage graph is unreadable.
        """
        if key not in self.graph:
            return nx.DiGraph()
        keep = {key}
        if direction in ("up", "both"):
            keep |= set(self.dependencies(key, hops=radius))
        if direction in ("down", "both"):
            keep |= set(self.dependents(key, hops=radius))
        return self.graph.subgraph(keep).copy()

    def _walk(self, key: str, step, hops: int | None) -> tuple[str, ...]:
        if key not in self.graph:
            return ()
        seen: dict[str, None] = {}
        frontier = [key]
        depth = 0
        while frontier and (hops is None or depth < hops):
            following: list[str] = []
            for node in frontier:
                for neighbour in step(node):
                    if neighbour not in seen and neighbour != key:
                        seen[neighbour] = None
                        following.append(neighbour)
            frontier = following
            depth += 1
        return tuple(seen)

    # -- inspection ----------------------------------------------------------------------

    def edge_pairs(self) -> set[tuple[str, str]]:
        """(dependency, dependent) pairs. The comparable form for an engine diff."""
        return {(u, v) for u, v in self.graph.edges()}

    def edge_table(self) -> pd.DataFrame:
        import pandas as pd

        return pd.DataFrame.from_records(
            [
                {"dependency": u, "dependent": v, "kind": data.get("kind"), "via": data.get("via")}
                for u, v, data in self.graph.edges(data=True)
            ],
            columns=["dependency", "dependent", "kind", "via"],
        )

    def __repr__(self) -> str:
        return (
            f"<LineageGraph nodes={self.graph.number_of_nodes()} "
            f"edges={self.graph.number_of_edges()} "
            f"unresolved={len(self.unresolved)} gaps={len(self.gaps)}>"
        )


def build_lineage(model: Model) -> LineageGraph:
    """Walk every expression the Model carries and assemble the graph."""
    model.require("expressions")

    graph = nx.DiGraph()
    unresolved: list[UnresolvedRef] = []
    notes: list[str] = []

    # -- nodes ---------------------------------------------------------------------------
    for table in model.tables.values():
        graph.add_node(
            table.key,
            kind="table",
            name=table.name,
            table=None,
            hidden=table.is_hidden,
            auto_date_time=table.is_auto_date_time,
        )
    for column in model.columns.values():
        if column.is_row_number:
            continue  # engine-internal; nothing can reference it
        graph.add_node(
            column.key,
            kind="calc_column" if column.is_calculated else "column",
            name=column.name,
            table=column.table,
            hidden=column.is_hidden,
            bytes=column.metrics.total_bytes if column.metrics else None,
        )
    for measure in model.measures.values():
        graph.add_node(
            measure.key,
            kind="measure",
            name=measure.name,
            table=measure.table,
            hidden=measure.is_hidden,
        )

    index = ModelIndex(model)

    # -- expression edges ----------------------------------------------------------------
    for measure in model.measures.values():
        _add_expression_edges(
            graph, index, measure.expression, measure.key, measure.table, unresolved, notes
        )
    for column in model.columns.values():
        if not column.is_calculated or not column.expression:
            continue
        _add_expression_edges(
            graph, index, column.expression, column.key, column.table, unresolved, notes
        )
    for table in model.tables.values():
        # Only calculated tables: a regular partition's source is M, and feeding Power
        # Query to a DAX extractor invents references rather than finding them.
        if not table.is_calculated or not table.source:
            continue
        _add_expression_edges(
            graph, index, table.source, table.key, table.name, unresolved, notes
        )
    for measure in model.measures.values():
        for expression in (
            measure.detail_rows,
            measure.format_expression,
            *measure.kpi_expressions,
        ):
            if not expression:
                continue
            _add_expression_edges(
                graph, index, expression, measure.key, measure.table, unresolved, notes
            )

    # -- row-level security --------------------------------------------------------------
    for role in model.roles:
        for permission in role.table_permissions:
            if not permission.filter_expression:
                continue  # whole-table access, not an expression
            graph.add_node(
                permission.key,
                kind="rls",
                name=f"{role.name} / {permission.table}",
                table=permission.table,
                hidden=False,
            )
            _add_expression_edges(
                graph,
                index,
                permission.filter_expression,
                permission.key,
                permission.table,
                unresolved,
                notes,
            )

    # -- calculation items -----------------------------------------------------------------
    for item in model.calculation_items:
        graph.add_node(
            item.key, kind="calc_item", name=item.name, table=item.table, hidden=False
        )
        for expression in (item.expression, item.format_expression):
            _add_expression_edges(
                graph, index, expression, item.key, item.table, unresolved, notes
            )

    # -- hierarchies -------------------------------------------------------------------------
    for hierarchy in model.hierarchies:
        graph.add_node(
            hierarchy.key,
            kind="hierarchy",
            name=hierarchy.name,
            table=hierarchy.table,
            hidden=hierarchy.is_hidden,
        )
        for level in hierarchy.levels:
            if not level.column:
                continue
            column = index.column(hierarchy.table, level.column)
            if column is None:
                unresolved.append(
                    UnresolvedRef(
                        hierarchy.key,
                        f"{hierarchy.table}[{level.column}]",
                        "hierarchy level names a column the table does not have",
                    )
                )
                continue
            graph.add_edge(column.key, hierarchy.key, kind="hierarchy", via="level")

    # -- relationship edges ---------------------------------------------------------------
    for relationship in model.relationships:
        source, target = relationship.endpoints
        node = (
            f"{source.key} -> {target.key}"
            if relationship.is_active
            else f"{source.key} -> {target.key} (inactive)"
        )
        graph.add_node(
            node,
            kind="relationship",
            name=node,
            table=None,
            active=relationship.is_active,
        )
        for endpoint in (source, target):
            if endpoint.key in graph:
                graph.add_edge(endpoint.key, node, kind="relationship", via="endpoint")
            else:
                unresolved.append(
                    UnresolvedRef(node, endpoint.key, "relationship endpoint is not a known column")
                )

    # -- sort-by and group-by edges ----------------------------------------------------------
    for column in model.columns.values():
        if column.sort_by and column.sort_by in graph:
            graph.add_edge(column.sort_by, column.key, kind="sort_by", via="sortByColumn")
        # A field parameter's visible column keeps its hidden grouping column alive, and
        # nothing else in the model or the report names that column.
        if column.group_by and column.group_by in graph:
            graph.add_edge(column.group_by, column.key, kind="group_by", via="groupByColumn")

    gaps = [f"{what} are not loaded — {consequence}" for what, consequence in _STRUCTURAL_GAPS]
    if not model.has("report"):
        gaps.append(
            "report field bindings are not loaded — an object used only by a visual, "
            "filter, conditional format or bookmark will look unreferenced"
        )

    return LineageGraph(
        graph=graph,
        unresolved=tuple(unresolved),
        notes=tuple(dict.fromkeys(notes)),
        gaps=tuple(gaps),
    )


def _add_expression_edges(
    graph: nx.DiGraph,
    index: ModelIndex,
    expression: str | None,
    owner: str,
    context_table: str | None,
    unresolved: list[UnresolvedRef],
    notes: list[str],
) -> None:
    if not expression:
        return
    from dax_quax.dax.resolve import resolve

    resolution: Resolution = resolve(extract(expression), index, context_table)
    for reference in resolution.references:
        if reference.key == owner:
            continue  # a measure naming itself; not an edge worth keeping
        if reference.key not in graph:
            continue
        graph.add_edge(reference.key, owner, kind="expression", via=reference.via)
    for miss in resolution.unresolved:
        unresolved.append(UnresolvedRef(owner, miss.text, miss.reason))
    notes.extend(resolution.notes)


# -- comparison with the engine's own answer -------------------------------------------------

#: DISCOVER_CALC_DEPENDENCY object types this graph models. Anything else in that DMV is a
#: known gap rather than a disagreement.
_ENGINE_KINDS = {
    "MEASURE": "measure",
    "CALC_COLUMN": "column",
    "COLUMN": "column",
    "CALC_TABLE": "table",
    "TABLE": "table",
}


def _engine_key(object_type: str | None, table: str | None, name: str | None) -> str | None:
    kind = _ENGINE_KINDS.get((object_type or "").upper())
    if kind is None or not name:
        return None
    if kind == "measure":
        return f"[{name}]"
    if kind == "table":
        return name
    return f"{table}[{name}]" if table else None


def calc_dependency_pairs(rows: list[dict]) -> set[tuple[str, str]]:
    """Turn DISCOVER_CALC_DEPENDENCY rows into (dependency, dependent) pairs.

    Rows naming object types this graph does not model are dropped, so the diff reflects
    real disagreement rather than known scope.
    """
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        dependent = _engine_key(row.get("OBJECT_TYPE"), row.get("TABLE"), row.get("OBJECT"))
        dependency = _engine_key(
            row.get("REFERENCED_OBJECT_TYPE"),
            row.get("REFERENCED_TABLE"),
            row.get("REFERENCED_OBJECT"),
        )
        if dependent and dependency and dependent != dependency:
            pairs.add((dependency, dependent))
    return pairs


def diff_against_engine(
    lineage: LineageGraph, rows: list[dict]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Compare our expression edges with the engine's own dependency list.

    Returns ``(missed, invented)`` — edges the engine found that we did not, and edges we
    claimed that it did not. **M2 is done when both are empty on a real model.** Missed
    edges are the dangerous half: each one is a potential false "unused" verdict.

    Only expression edges are compared. Relationship and sort-by edges are ours alone.
    """
    ours = {
        (u, v)
        for u, v, data in lineage.graph.edges(data=True)
        if data.get("kind") == "expression"
    }
    theirs = calc_dependency_pairs(rows)
    known = set(lineage.graph.nodes)
    theirs = {(u, v) for u, v in theirs if u in known and v in known}
    return theirs - ours, ours - theirs
