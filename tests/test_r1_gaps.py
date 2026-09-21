"""R1: the four expression sources that used to be invisible.

Each of these was a way for a column to be genuinely in use while the tool reported it as
unreferenced — the exact failure the whole project exists to avoid. The `rich` fixture is
built so that each new column is kept alive by *exactly one* of them, so a regression in any
single walk shows up as that column turning REMOVE rather than being masked by another edge.

    Customer[Region]     only a row-level-security filter references it
    Product[Category]    only a hierarchy level references it
    [Margin % (old)]     only a calculation item references it
    Customer[Email]      only a detail rows expression references it
"""

from __future__ import annotations

import pytest

from dax_quax.analysis.usage import Verdict, assess

#: object -> the one mechanism keeping it alive
SOLE_KEEPER = {
    "Customer[Region]": "RLS Sales Rep/Customer",
    "Product[Category]": "hierarchy Product[Product Hierarchy]",
    "[Margin % (old)]": "calculation item Time Intelligence[Prior year margin]",
    "Customer[Email]": "[Total Sales]",
}


# -- loading ---------------------------------------------------------------------------


def test_roles_and_their_filters_load(rich_model):
    roles = {role.name: role for role in rich_model.roles}
    assert set(roles) == {"Sales Rep", "Auditor"}
    permission = roles["Sales Rep"].table_permissions[0]
    assert permission.table == "Customer"
    assert "USERPRINCIPALNAME" in permission.filter_expression


def test_a_permission_with_no_filter_is_whole_table_access(rich_model):
    """Not every table permission is an expression; a filterless one grants the lot."""
    auditor = next(r for r in rich_model.roles if r.name == "Auditor")
    assert auditor.table_permissions[0].filter_expression is None


def test_hierarchies_and_levels_load(rich_model):
    hierarchy = rich_model.hierarchies[0]
    assert hierarchy.key == "hierarchy Product[Product Hierarchy]"
    assert [(level.name, level.column) for level in hierarchy.levels] == [
        ("Category", "Category"),
        ("Product", "Product Name"),
    ]


def test_calculation_items_load(rich_model):
    items = {item.name: item for item in rich_model.calculation_items}
    assert set(items) == {"Prior year margin", "Current"}
    assert items["Prior year margin"].table == "Time Intelligence"
    assert "[Margin % (old)]" in items["Prior year margin"].expression


def test_detail_rows_load(rich_model):
    assert "Customer[Email]" in rich_model.measures["[Total Sales]"].detail_rows


def test_the_rich_fixture_parses_with_nothing_left_over(rich_model):
    assert not any("not understood" in w for w in rich_model.warnings)


# -- the point: each object is kept alive, by exactly one thing ----------------------------


@pytest.mark.parametrize(("key", "keeper"), sorted(SOLE_KEEPER.items()))
def test_each_object_is_referenced_by_its_one_keeper(rich_model, key, keeper):
    lineage = rich_model.lineage()
    assert lineage.is_referenced(key), f"{key} reads as unreferenced"
    assert keeper in lineage.dependents(key, hops=1)


@pytest.mark.parametrize("key", sorted(SOLE_KEEPER))
def test_none_of_them_is_reported_removable(rich_model, key):
    from dax_quax.report import ReportBindings, attach_report

    # A report has to be attached or everything is UNKNOWN and the test proves nothing.
    attach_report(rich_model, ReportBindings(name="Empty.Report"))
    findings = {f.key: f for f in assess(rich_model)}
    assert findings[key].verdict is not Verdict.REMOVE


def test_removing_a_single_keeper_is_what_turns_it_removable(rich_model):
    """Proves the edge is doing the work, rather than something else masking it."""
    from dax_quax.report import ReportBindings, attach_report

    attach_report(rich_model, ReportBindings(name="Empty.Report"))
    assert {f.key: f for f in assess(rich_model)}["Customer[Region]"].verdict is not (
        Verdict.REMOVE
    )

    rich_model.roles = []  # drop row-level security, and nothing else
    assert {f.key: f for f in assess(rich_model)}["Customer[Region]"].verdict is Verdict.REMOVE


def test_a_filterless_permission_creates_no_edge(rich_model):
    """The Auditor role grants whole-table access to Sales. That is not a reference."""
    lineage = rich_model.lineage()
    assert not any(node.startswith("RLS Auditor") for node in lineage.graph.nodes)


def test_a_hierarchy_level_naming_a_missing_column_is_reported(rich_model):
    from dax_quax.analysis.lineage import build_lineage
    from dax_quax.model import Level

    rich_model.hierarchies[0].levels.append(Level(name="Ghost", column="Nope"))
    lineage = build_lineage(rich_model)
    assert any("hierarchy level names a column" in u.reason for u in lineage.unresolved)


# -- both sources agree ------------------------------------------------------------------------


def test_live_and_pbip_agree_on_the_rich_model(rich_model, rich_live_model):
    """The M3 acceptance test, extended to the four new object kinds.

    They share only the DAX layer, so an edge in one and not the other means a loader is
    dropping one of the sources R1 just added.
    """
    assert rich_live_model.lineage().edge_pairs() == rich_model.lineage().edge_pairs()


@pytest.mark.parametrize("key", sorted(SOLE_KEEPER))
def test_both_sources_keep_the_same_objects_alive(rich_model, rich_live_model, key):
    assert rich_live_model.lineage().is_referenced(key)
    assert rich_model.lineage().is_referenced(key)


def test_the_dmv_loader_reads_all_four(rich_live_model):
    assert {r.name for r in rich_live_model.roles} == {"Sales Rep", "Auditor"}
    assert rich_live_model.hierarchies[0].levels[0].column == "Category"
    assert len(rich_live_model.calculation_items) == 2
    assert rich_live_model.measures["[Total Sales]"].detail_rows


def test_an_unattachable_detail_rows_expression_is_reported():
    """Its columns would look unreferenced, so it must not be dropped in silence."""
    from dax_quax.sources import dmv

    model = dmv.build_model(
        {
            "tables": [{"ID": 1, "Name": "Sales", "IsHidden": False}],
            "columns": [],
            "measures": [],
            "detail_rows": [{"MeasureID": 999, "Expression": "SELECTCOLUMNS ( Sales )"}],
        },
        name="x",
    )
    assert any("could not be tied to a measure" in w for w in model.warnings)
