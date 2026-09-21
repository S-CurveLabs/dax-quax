from __future__ import annotations

import pandas as pd
import pytest

from dax_quax.analysis.vertipaq import concentration, table_summary, vertipaq
from dax_quax.errors import MissingCapability


def test_frame_is_sorted_largest_first(synthetic_model):
    frame = vertipaq(synthetic_model)
    assert frame.iloc[0]["column"] == "Order Number"
    assert frame.iloc[0]["total_bytes"] == 7025
    sizes = frame["total_bytes"].dropna().tolist()
    assert sizes == sorted(sizes, reverse=True)


def test_row_number_columns_are_excluded_by_default(synthetic_model):
    assert "row_number" not in set(vertipaq(synthetic_model)["kind"])
    assert "row_number" in set(vertipaq(synthetic_model, include_row_number=True)["kind"])


def test_unmeasured_column_is_null_not_zero(synthetic_model):
    frame = vertipaq(synthetic_model).set_index(["table", "column"])
    row = frame.loc[("Product", "Product Key")]
    assert pd.isna(row["total_bytes"])
    assert pd.isna(row["cardinality"])
    # and it sorts last rather than to the bottom of the size ranking as a zero
    assert vertipaq(synthetic_model).iloc[-1]["column"] == "Product Key"


def test_pct_is_share_of_the_whole_model(synthetic_model):
    frame = vertipaq(synthetic_model).set_index(["table", "column"])
    expected = 7025 / 8265 * 100
    assert frame.loc[("Sales", "Order Number"), "pct_of_model"] == pytest.approx(expected)


def test_calculated_column_kind(synthetic_model):
    frame = vertipaq(synthetic_model).set_index(["table", "column"])
    assert frame.loc[("Sales", "Line Amount"), "kind"] == "calculated"
    assert frame.loc[("Sales", "Quantity"), "kind"] == "column"


def test_table_summary_rolls_up(synthetic_model):
    summary = table_summary(synthetic_model).set_index("table")
    # Sales: 7025 + 300 + 420 + 230 + 10 (row number) = 7985
    assert summary.loc["Sales", "total_bytes"] == 7985
    assert summary.loc["Sales", "row_number_bytes"] == 10
    assert summary.loc["Sales", "rows"] == 1000
    # Product has one column the source could not measure
    assert summary.loc["Product", "columns"] == 4
    assert summary.loc["Product", "measured_columns"] == 3
    assert summary.loc["LocalDateTable_8f3c1e2a-0000-4000-9000-1234567890ab", "auto_date_time"]


def test_table_totals_reconcile_with_the_model_total(synthetic_model):
    """Every measured byte is attributable to exactly one table, row numbers included."""
    summary = table_summary(synthetic_model)
    assert summary["total_bytes"].sum() == synthetic_model.total_bytes
    assert summary["pct_of_model"].sum() == pytest.approx(100.0)


def test_concentration_headline(synthetic_model):
    head, pct = concentration(synthetic_model, top=2)
    assert head == 7025 + 420  # Order Number + Line Amount
    assert pct == pytest.approx(head / 8265 * 100)


def test_vertipaq_refuses_a_model_without_metrics(metadata_only_model):
    with pytest.raises(MissingCapability):
        vertipaq(metadata_only_model)
