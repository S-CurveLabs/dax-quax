"""Lineage graph tests.

The synthetic fixture forms one real chain, which is what most of these exercise:

    Sales[Quantity] ─▶ Sales[Line Amount] ─▶ [Total Sales] ─▶ [Margin % (old)]
    Product[List Price] ─▶ Sales[Line Amount]
    Sales (table) ─▶ [Total Sales]
"""

from __future__ import annotations

import pytest

from dax_quax.analysis.lineage import build_lineage, calc_dependency_pairs, diff_against_engine
from dax_quax.errors import MissingCapability


@pytest.fixture
def lineage(synthetic_model):
    return build_lineage(synthetic_model)


# -- nodes -------------------------------------------------------------------------------


def test_every_user_object_is_a_node(lineage):
    nodes = set(lineage.graph.nodes)
    assert {"Sales", "Product", "Sales[Quantity]", "[Total Sales]"} <= nodes


def test_row_number_columns_are_not_nodes(lineage):
    """Engine-internal, and nothing can reference it, so it would only ever read as unused."""
    assert not any("RowNumber-" in node for node in lineage.graph.nodes)


def test_node_carries_kind_and_size(lineage):
    data = lineage.graph.nodes["Sales[Line Amount]"]
    assert data["kind"] == "calc_column"
    assert data["table"] == "Sales"
    assert data["bytes"] == 420


# -- expression edges ----------------------------------------------------------------------


def test_direct_dependents(lineage):
    assert set(lineage.dependents("Sales[Quantity]", hops=1)) == {
        "Sales[Line Amount]",
        "[Total Quantity]",
    }


def test_dependents_are_transitive_by_default(lineage):
    downstream = set(lineage.dependents("Sales[Quantity]"))
    assert {"Sales[Line Amount]", "[Total Sales]", "[Margin % (old)]"} <= downstream


def test_hops_limits_the_walk(lineage):
    assert "[Total Sales]" not in lineage.dependents("Sales[Quantity]", hops=1)
    assert "[Total Sales]" in lineage.dependents("Sales[Quantity]", hops=2)


def test_dependencies_walk_the_other_way(lineage):
    upstream = set(lineage.dependencies("[Margin % (old)]"))
    assert {"[Total Sales]", "Sales[Line Amount]", "Sales[Quantity]", "Product[List Price]"} <= (
        upstream
    )


def test_a_referenced_table_is_an_edge(lineage):
    """SUMX ( Sales, ... ) depends on the table itself, not only on its columns."""
    assert "[Total Sales]" in lineage.dependents("Sales", hops=1)


def test_upstream_and_downstream_are_aliases(lineage):
    assert lineage.upstream("[Total Sales]") == lineage.dependencies("[Total Sales]")
    assert lineage.downstream("Sales[Quantity]") == lineage.dependents("Sales[Quantity]")


# -- non-expression edges --------------------------------------------------------------------


def test_relationship_endpoints_are_referenced(lineage):
    """A column used only as a relationship endpoint is in use."""
    assert lineage.is_referenced("Sales[Product Key]")
    assert lineage.is_referenced("Product[Product Key]")
    relationship = next(
        node for node, data in lineage.graph.nodes(data=True) if data["kind"] == "relationship"
    )
    assert lineage.graph.edges["Sales[Product Key]", relationship]["kind"] == "relationship"


def test_sort_by_column_is_referenced(lineage):
    """Product[Sort Order] exists only to order another column. It is still in use."""
    assert lineage.is_referenced("Product[Sort Order]")
    assert "Product[Product Name]" in lineage.dependents("Product[Sort Order]", hops=1)


def test_relationship_node_does_not_create_a_cycle(lineage):
    import networkx as nx

    assert nx.is_directed_acyclic_graph(lineage.graph)


# -- what is actually unreferenced --------------------------------------------------------------


def test_a_genuinely_unreferenced_column(lineage):
    assert not lineage.is_referenced("Sales[Order Number]")
    assert lineage.reference_count("Sales[Order Number]") == 0


def test_reference_count_counts_dependents(lineage):
    assert lineage.reference_count("Sales[Quantity]") == 2


# -- honesty about what the graph cannot see --------------------------------------------------


def test_gaps_are_declared(lineage):
    """A zero-degree node is only meaningful alongside this list."""
    joined = " ".join(lineage.gaps)
    assert "perspectives" in joined
    assert "translations" in joined
    assert "report field bindings" in joined  # synthetic model has no report capability


def test_the_r1_gaps_are_closed(lineage):
    """RLS, calculation items, hierarchies and detail rows were blind spots until R1.

    Each was a way for a column to be in use while reading as unreferenced. They are
    walked now, so they must not still be advertised as things the graph cannot see.
    """
    joined = " ".join(lineage.gaps)
    for closed in ("RLS", "calculation item", "hierarchies", "detail rows"):
        assert closed not in joined, f"{closed} is walked now but still listed as a gap"


def test_report_gap_disappears_when_the_report_layer_is_present(synthetic_model):
    synthetic_model.capabilities = synthetic_model.capabilities | {"report"}
    assert not any("report field bindings" in gap for gap in build_lineage(synthetic_model).gaps)


def test_unresolvable_references_are_recorded(synthetic_model):
    synthetic_model.measures["[Total Sales]"].expression = "SUM ( Sales[Ghost] )"
    lineage = build_lineage(synthetic_model)
    assert any(u.owner == "[Total Sales]" and u.text == "Sales[Ghost]" for u in lineage.unresolved)


def test_lineage_requires_expressions(synthetic_model):
    synthetic_model.capabilities = frozenset({"metadata"})
    with pytest.raises(MissingCapability):
        build_lineage(synthetic_model)


def test_a_self_referencing_measure_makes_no_self_edge(synthetic_model):
    synthetic_model.measures["[Total Sales]"].expression = "[Total Sales] + 1"
    lineage = build_lineage(synthetic_model)
    assert not lineage.graph.has_edge("[Total Sales]", "[Total Sales]")


# -- neighbourhood ------------------------------------------------------------------------------


def test_neighborhood_is_bounded(lineage):
    near = lineage.neighborhood("[Total Sales]", radius=1)
    assert "[Margin % (old)]" in near
    assert "Product[List Price]" not in near  # two hops upstream


def test_neighborhood_direction(lineage):
    down = lineage.neighborhood("Sales[Quantity]", radius=3, direction="down")
    assert "[Margin % (old)]" in down
    up = lineage.neighborhood("Sales[Quantity]", radius=3, direction="up")
    assert set(up.nodes) == {"Sales[Quantity]"}


def test_neighborhood_of_an_unknown_key_is_empty(lineage):
    assert lineage.neighborhood("Nope[Nope]").number_of_nodes() == 0


def test_edge_table_shape(lineage):
    frame = lineage.edge_table()
    assert list(frame.columns) == ["dependency", "dependent", "kind", "via"]
    assert set(frame["kind"]) == {"expression", "relationship", "sort_by"}


# -- the M2 acceptance mechanism -------------------------------------------------------------------


def engine_rows_matching(lineage) -> list[dict]:
    """Synthesise DISCOVER_CALC_DEPENDENCY rows that agree with our expression edges."""
    kind_to_engine = {
        "measure": "MEASURE",
        "calc_column": "CALC_COLUMN",
        "column": "COLUMN",
        "table": "TABLE",
    }
    rows = []
    for dependency, dependent, data in lineage.graph.edges(data=True):
        if data.get("kind") != "expression":
            continue
        a, b = lineage.graph.nodes[dependency], lineage.graph.nodes[dependent]
        rows.append(
            {
                "OBJECT_TYPE": kind_to_engine[b["kind"]],
                "TABLE": b["table"] or b["name"],
                "OBJECT": b["name"],
                "REFERENCED_OBJECT_TYPE": kind_to_engine[a["kind"]],
                "REFERENCED_TABLE": a["table"] or a["name"],
                "REFERENCED_OBJECT": a["name"],
            }
        )
    return rows


def test_diff_is_empty_when_the_engine_agrees(lineage):
    missed, invented = diff_against_engine(lineage, engine_rows_matching(lineage))
    assert missed == set()
    assert invented == set()


def test_diff_reports_an_edge_the_engine_found_that_we_missed(lineage):
    rows = engine_rows_matching(lineage)
    rows.append(
        {
            "OBJECT_TYPE": "MEASURE",
            "TABLE": "Sales",
            "OBJECT": "Total Quantity",
            "REFERENCED_OBJECT_TYPE": "COLUMN",
            "REFERENCED_TABLE": "Sales",
            "REFERENCED_OBJECT": "Order Number",
        }
    )
    missed, invented = diff_against_engine(lineage, rows)
    assert missed == {("Sales[Order Number]", "[Total Quantity]")}
    assert invented == set()


def test_diff_reports_an_edge_we_invented(lineage):
    rows = engine_rows_matching(lineage)[1:]
    missed, invented = diff_against_engine(lineage, rows)
    assert invented
    assert missed == set()


def test_unmodelled_engine_object_types_are_dropped():
    """M_EXPRESSION and friends are out of scope, not a disagreement."""
    rows = [
        {
            "OBJECT_TYPE": "M_EXPRESSION",
            "TABLE": "Sales",
            "OBJECT": "Query",
            "REFERENCED_OBJECT_TYPE": "M_EXPRESSION",
            "REFERENCED_TABLE": None,
            "REFERENCED_OBJECT": "Source",
        }
    ]
    assert calc_dependency_pairs(rows) == set()


def test_calc_dependency_pairs_builds_canonical_keys():
    rows = [
        {
            "OBJECT_TYPE": "MEASURE",
            "TABLE": "Sales",
            "OBJECT": "Total Sales",
            "REFERENCED_OBJECT_TYPE": "CALC_COLUMN",
            "REFERENCED_TABLE": "Sales",
            "REFERENCED_OBJECT": "Line Amount",
        }
    ]
    assert calc_dependency_pairs(rows) == {("Sales[Line Amount]", "[Total Sales]")}


# -- calculated tables and field parameters (code review, section C) ------------------------


def _with_calculated_table(model, name: str, dax: str):
    from dax_quax.model import Table

    model.tables[name] = Table(name=name, is_calculated=True, source=dax)
    return model


def test_a_calculated_table_expression_is_walked(synthetic_model):
    """Its references were invisible, so a column used only by one read as unused."""
    model = _with_calculated_table(
        synthetic_model, "Top Products", "TOPN ( 10, Product, Product[List Price] )"
    )
    lineage = build_lineage(model)
    assert "Top Products" in lineage.dependents("Product[List Price]")


def test_a_field_parameter_keeps_its_columns_alive(synthetic_model):
    """A field parameter is a calculated table full of NAMEOF(...).

    The extractor always handled NAMEOF; nothing ever handed it the expression, so the
    columns a field parameter exposes still read as unreferenced.
    """
    model = _with_calculated_table(
        synthetic_model,
        "Fields",
        '{ ("Order no", NAMEOF ( Sales[Order Number] ), 0), '
        '("Qty", NAMEOF ( Sales[Quantity] ), 1) }',
    )
    lineage = build_lineage(model)
    assert lineage.is_referenced("Sales[Order Number]")
    assert "Fields" in lineage.dependents("Sales[Order Number]")


def test_m_source_is_not_fed_to_the_dax_extractor(synthetic_model):
    """A regular partition's source is Power Query. Walking it would invent references."""
    from dax_quax.model import Table

    synthetic_model.tables["Staging"] = Table(
        name="Staging",
        is_calculated=False,
        source='let Sales = Sql.Database("srv", "db") in Sales',
    )
    lineage = build_lineage(synthetic_model)
    assert "Staging" not in lineage.dependents("Sales")
