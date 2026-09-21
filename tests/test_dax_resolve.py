from __future__ import annotations

import pytest

from dax_quax.dax.resolve import ModelIndex, references_in
from dax_quax.model import Column, Measure, Model, Table


def mini(**kwargs) -> Model:
    """A two-table model built inline, for cases the shared fixture does not cover."""
    model = Model(name="Mini", source="test", capabilities=frozenset({"metadata", "expressions"}))
    for name in ("Sales", "Product"):
        model.tables[name] = Table(name=name)
    for table, column in (
        ("Sales", "Amount"),
        ("Sales", "Quantity"),
        ("Product", "Color"),
    ):
        model.columns[f"{table}[{column}]"] = Column(table=table, name=column)
    for measure in ("Total Sales",):
        model.measures[f"[{measure}]"] = Measure(table="Sales", name=measure)
    for table, column in kwargs.get("extra_columns", []):
        model.columns[f"{table}[{column}]"] = Column(table=table, name=column)
    for measure in kwargs.get("extra_measures", []):
        model.measures[f"[{measure}]"] = Measure(table="Sales", name=measure)
    return model


# -- the straightforward cases -------------------------------------------------------------


def test_qualified_column_resolves(synthetic_model):
    resolution = references_in("SUM ( Sales[Quantity] )", synthetic_model)
    assert resolution.keys == ("Sales[Quantity]",)
    assert resolution.references[0].via == "qualified"


def test_lookups_are_case_insensitive(synthetic_model):
    """DAX does not care about case, and real models are inconsistent about it."""
    resolution = references_in("sum ( sALES[order NUMBER] )", synthetic_model)
    assert resolution.keys == ("Sales[Order Number]",)


def test_bare_bracket_resolves_to_a_measure(synthetic_model):
    resolution = references_in("[Total Sales] * 2", synthetic_model)
    assert resolution.keys == ("[Total Sales]",)
    assert resolution.references[0].via == "measure"


def test_bare_bracket_resolves_to_a_column_of_the_surrounding_table(synthetic_model):
    resolution = references_in("SUMX ( Sales, [Quantity] )", synthetic_model, context_table="Sales")
    assert "Sales[Quantity]" in resolution.keys
    assert any(r.via == "context_column" for r in resolution.references)


def test_bare_table_resolves(synthetic_model):
    resolution = references_in("COUNTROWS ( Product )", synthetic_model)
    assert resolution.keys == ("Product",)
    assert resolution.references[0].via == "table"


# -- the ambiguous ones ---------------------------------------------------------------------


def test_a_measure_wins_over_a_same_named_column():
    """Measure names are unique model-wide; this is how the engine resolves it too."""
    model = mini(extra_measures=["Amount"])
    resolution = references_in("[Amount] + 1", model, context_table="Sales")
    assert resolution.keys == ("[Amount]",)
    assert resolution.references[0].via == "measure"


def test_table_qualified_measure_resolves_with_a_note():
    """Legal, discouraged — but losing the edge would produce a false 'unused' later."""
    model = mini()
    resolution = references_in("Sales[Total Sales] * 2", model)
    assert resolution.keys == ("[Total Sales]",)
    assert resolution.references[0].via == "table_qualified_measure"
    assert any("table-qualified measure" in note for note in resolution.notes)


def test_unqualified_bracket_without_context_cannot_reach_a_column():
    model = mini()
    resolution = references_in("[Amount] + 1", model)
    assert resolution.keys == ()
    assert resolution.unresolved[0].text == "[Amount]"
    assert "no surrounding table" in resolution.unresolved[0].reason


# -- failure classification ------------------------------------------------------------------


def test_unknown_bracket_is_unresolved_and_reported(synthetic_model):
    resolution = references_in("[No Such Measure] + 1", synthetic_model)
    assert resolution.references == ()
    assert len(resolution.unresolved) == 1
    assert resolution.unresolved[0].text == "[No Such Measure]"


def test_unknown_bare_identifier_is_ignored_not_unresolved(synthetic_model):
    """Bare words that match no table are almost always enum constants, not lost edges."""
    resolution = references_in(
        "DATEADD ( 'Date'[Date], -1, MONTH )", synthetic_model
    )
    assert "MONTH" in resolution.ignored
    assert all(u.text != "MONTH" for u in resolution.unresolved)


def test_a_missing_column_on_a_real_table_is_unresolved(synthetic_model):
    resolution = references_in("Sales[Gone]", synthetic_model)
    assert resolution.unresolved[0].text == "Sales[Gone]"
    assert resolution.references == ()


# -- index reuse -------------------------------------------------------------------------------


def test_index_is_reusable_across_expressions(synthetic_model):
    from dax_quax.dax.extract import extract
    from dax_quax.dax.resolve import resolve

    index = ModelIndex(synthetic_model)
    first = resolve(extract("SUM ( Sales[Quantity] )"), index)
    second = resolve(extract("[Total Sales]"), index)
    assert first.keys == ("Sales[Quantity]",)
    assert second.keys == ("[Total Sales]",)


def test_keys_are_deduplicated_in_first_seen_order(synthetic_model):
    resolution = references_in(
        "Sales[Quantity] + Sales[Order Number] + Sales[Quantity]", synthetic_model
    )
    assert resolution.keys == ("Sales[Quantity]", "Sales[Order Number]")


# -- the fixture's own expressions resolve cleanly ------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "context", "expected"),
    [
        ("SUM ( Sales[Quantity] )", "Sales", {"Sales[Quantity]"}),
        ("SUMX ( Sales, Sales[Line Amount] )", "Sales", {"Sales", "Sales[Line Amount]"}),
        ("DIVIDE ( [Total Sales], 1 )", "Sales", {"[Total Sales]"}),
        (
            "Sales[Quantity] * RELATED ( Product[List Price] )",
            "Sales",
            {"Sales[Quantity]", "Product[List Price]"},
        ),
    ],
)
def test_fixture_expressions_resolve_without_loss(synthetic_model, expression, context, expected):
    resolution = references_in(expression, synthetic_model, context_table=context)
    assert set(resolution.keys) == expected
    assert resolution.unresolved == ()
