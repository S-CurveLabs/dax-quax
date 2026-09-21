"""Tests for the DMV -> Model transformation.

These validate plumbing and arithmetic against a SYNTHETIC fixture. They do not confirm
that a real engine uses these column names or join keys — that is what verify_contract()
and the live tests are for.
"""

from __future__ import annotations

import pytest

from dax_quax.sources import dmv

# -- structure --------------------------------------------------------------------------


def test_tables_and_columns_land(synthetic_model):
    assert set(synthetic_model.tables) == {
        "Sales",
        "Product",
        "LocalDateTable_8f3c1e2a-0000-4000-9000-1234567890ab",
    }
    assert "Sales[Order Number]" in synthetic_model.columns
    assert "Product[List Price]" in synthetic_model.columns


def test_measure_keys_omit_the_home_table(synthetic_model):
    assert "[Total Sales]" in synthetic_model.measures
    assert synthetic_model.measures["[Total Sales]"].table == "Sales"


def test_calculated_and_row_number_columns_are_flagged(synthetic_model):
    assert synthetic_model.columns["Sales[Line Amount]"].is_calculated
    assert not synthetic_model.columns["Sales[Quantity]"].is_calculated
    row_numbers = [c for c in synthetic_model.columns.values() if c.is_row_number]
    assert len(row_numbers) == 1


def test_auto_date_time_table_recognised(synthetic_model):
    auto = synthetic_model.tables["LocalDateTable_8f3c1e2a-0000-4000-9000-1234567890ab"]
    assert auto.is_auto_date_time
    assert not synthetic_model.tables["Sales"].is_auto_date_time


def test_sort_by_is_resolved_to_a_key(synthetic_model):
    """A column used only as another column's sort order is still in use."""
    assert synthetic_model.columns["Product[Product Name]"].sort_by == "Product[Sort Order]"
    assert synthetic_model.columns["Product[Sort Order]"].sort_by is None


def test_relationship_endpoints_resolve(synthetic_model):
    assert len(synthetic_model.relationships) == 1
    rel = synthetic_model.relationships[0]
    assert (rel.from_table, rel.from_column) == ("Sales", "Product Key")
    assert (rel.to_table, rel.to_column) == ("Product", "Product Key")
    assert rel.is_active


def test_partition_mode_applied(synthetic_model):
    assert synthetic_model.tables["Sales"].mode == "Import"


# -- unresolvable input is reported, never silently dropped -------------------------------


def test_orphan_column_produces_a_warning(synthetic_model):
    assert any("unknown TableID" in w for w in synthetic_model.warnings)
    assert not any(c.name == "Orphan" for c in synthetic_model.columns.values())


def test_unresolvable_relationship_produces_a_warning(synthetic_model):
    assert any("relationship with unresolvable endpoints" in w for w in synthetic_model.warnings)


# -- metrics ------------------------------------------------------------------------------


def test_metrics_join_and_sum(synthetic_model):
    metrics = synthetic_model.columns["Sales[Order Number]"].metrics
    assert metrics is not None
    assert metrics.dictionary_bytes == 5000
    assert metrics.data_bytes == 2000  # two segments summed
    assert metrics.hierarchy_bytes == 25  # from the H$Sales$Order Number segment row
    assert metrics.total_bytes == 7025
    # Not available from these rowsets; see test_cardinality_is_absent_rather_than_invented.
    assert metrics.cardinality is None
    assert metrics.encoding == "HASH"


def test_hierarchy_rows_do_not_leak_into_data_bytes(synthetic_model):
    """The H$ segment must be counted as hierarchy, not added to the column's data size."""
    assert synthetic_model.columns["Sales[Order Number]"].metrics.data_bytes == 2000


def test_unmeasured_column_has_none_not_zero(synthetic_model):
    """'We could not measure this' and 'this is empty' are different facts."""
    assert synthetic_model.columns["Product[Product Key]"].metrics is None
    assert synthetic_model.columns["Product[Sort Order]"].metrics.total_bytes == 60


def test_model_total_is_the_sum_of_measured_columns(synthetic_model):
    assert synthetic_model.total_bytes == 8265
    assert synthetic_model.measured_column_count == 8


def test_table_row_count_from_storage(synthetic_model):
    assert synthetic_model.tables["Sales"].row_count == 1000
    assert synthetic_model.tables["Product"].row_count == 40


# -- capabilities -------------------------------------------------------------------------


def test_capabilities_reflect_what_was_supplied(synthetic_model, metadata_only_model):
    assert "metrics" in synthetic_model.capabilities
    assert "execute" in synthetic_model.capabilities
    assert "metrics" not in metadata_only_model.capabilities
    assert "expressions" in metadata_only_model.capabilities


def test_require_names_the_missing_capability_and_a_remedy(metadata_only_model):
    from dax_quax.errors import MissingCapability

    with pytest.raises(MissingCapability) as excinfo:
        metadata_only_model.require("metrics")
    message = str(excinfo.value)
    assert "metrics" in message
    assert "cache.abf" in message  # the hint tells you where to get it
    assert excinfo.value.missing == ("metrics",)


# -- unverified joins fail loudly ----------------------------------------------------------


def test_storage_rows_that_match_nothing_raise_a_loud_warning(synthetic_rowsets):
    """If the join key assumption is wrong on a real engine, this is how we find out."""
    broken = dict(synthetic_rowsets)
    broken["storage_columns"] = [
        {**row, "ATTRIBUTE_NAME": f"XX_{row['ATTRIBUTE_NAME']}"}
        for row in broken["storage_columns"]
    ]
    broken["segments"] = [
        {**row, "COLUMN_ID": f"XX_{row['COLUMN_ID']}"} for row in broken["segments"]
    ]
    model = dmv.build_model(broken, name="Broken", source="live")
    assert any("JOIN 'column_storage' matched zero" in w for w in model.warnings)


def test_cardinality_is_absent_rather_than_invented(synthetic_model):
    """These rowsets do not carry it, and a real engine confirmed they never did.

    The old shape read cardinality from '$table$column' dictionary pseudo-tables. Power BI
    Desktop 2.157.1354.0 has no such rows: DISCOVER_STORAGE_TABLES holds the tables and
    the H$/U$/R$ structures and nothing else. The H$ row's ROWS_COUNT sits a consistent
    three above the distinct count, and a constant fitted to one model is not a
    measurement, so this stays None.
    """
    metrics = synthetic_model.columns["Sales[Order Number]"].metrics
    assert metrics is not None
    assert metrics.total_bytes > 0
    assert metrics.cardinality is None


def test_a_hierarchy_row_does_not_overwrite_a_real_column(synthetic_model):
    """Non-BASIC_DATA rows reuse a neighbouring column's ATTRIBUTE_NAME."""
    metrics = synthetic_model.columns["Sales[Order Number]"].metrics
    assert metrics.dictionary_bytes == 5000  # not the 999_999 on the hierarchy row


def test_an_id_suffix_is_stripped_from_identifiers():
    """The engine writes 'Calendar (10)', not 'Calendar'."""
    assert dmv.strip_id("Calendar (10)") == "Calendar"
    assert dmv.strip_id("Order Number (49)") == "Order Number"
    assert dmv.strip_id("Sales") == "Sales"
    assert dmv.strip_id("Q1 (2024)") == "Q1"  # indistinguishable, and the engine wins


# -- contract verification -----------------------------------------------------------------


def test_verify_contract_is_clean_on_the_fixture(synthetic_rowsets):
    findings = dmv.verify_contract(synthetic_rowsets)
    required = [f for f in findings if f.kind == "missing-required"]
    assert required == [], f"fixture does not satisfy the declared contract: {required}"


def test_verify_contract_notices_a_renamed_column(synthetic_rowsets):
    broken = dict(synthetic_rowsets)
    broken["tables"] = [
        {"ID": row["ID"], "TableName": row["Name"], "IsHidden": row["IsHidden"]}
        for row in broken["tables"]
    ]
    findings = dmv.verify_contract(broken)
    assert any(f.query == "tables" and f.kind == "missing-required" for f in findings)


def test_every_declared_query_has_a_statement():
    for name, query in dmv.QUERIES.items():
        assert query.statement.startswith("SELECT * FROM $SYSTEM."), name
        assert query.columns, f"{name} declares no columns it reads"


def test_an_empty_database_says_so_rather_than_looking_clean():
    """Power BI Desktop on a blank report serves a real, empty database.

    Found against a live instance: every DMV answered, none with rows, and the result was
    a Model claiming `metadata` and holding none -- which reads the same as a model whose
    objects we failed to parse.
    """
    from dax_quax.sources.dmv import build_model

    model = build_model({}, name="blank", source="live")
    assert model.tables == {}
    assert any("no tables" in w for w in model.warnings)


def test_a_populated_model_does_not_carry_that_warning(synthetic_rowsets):
    from dax_quax.sources.dmv import build_model

    model = build_model(synthetic_rowsets, name="m", source="replay")
    assert not any("no tables" in w for w in model.warnings)
