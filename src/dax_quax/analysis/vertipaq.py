"""VertiPaq size analysis. A pure function of a Model — no connection, no source knowledge."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dax_quax.model import Model

if TYPE_CHECKING:
    import pandas as pd

COLUMNS = (
    "table",
    "column",
    "kind",
    "hidden",
    "data_type",
    "encoding",
    "cardinality",
    "rows",
    "dictionary_bytes",
    "data_bytes",
    "hierarchy_bytes",
    "total_bytes",
    "pct_of_model",
)


def vertipaq(model: Model, *, include_row_number: bool = False) -> pd.DataFrame:
    """One row per column, largest first.

    Columns the source could not measure are included with null sizes rather than zeros —
    "we did not measure this" and "this is empty" are different facts.
    """
    import pandas as pd

    model.require("metrics")
    total = model.total_bytes

    records = []
    for column in model.columns.values():
        if column.is_row_number and not include_row_number:
            continue
        metrics = column.metrics
        measured = metrics is not None
        column_total = metrics.total_bytes if measured else None
        records.append(
            {
                "table": column.table,
                "column": column.name,
                "kind": _kind(column),
                "hidden": column.is_hidden,
                "data_type": column.data_type,
                "encoding": metrics.encoding if measured else None,
                "cardinality": metrics.cardinality if measured else None,
                "rows": metrics.rows if measured else None,
                "dictionary_bytes": metrics.dictionary_bytes if measured else None,
                "data_bytes": metrics.data_bytes if measured else None,
                "hierarchy_bytes": metrics.hierarchy_bytes if measured else None,
                "total_bytes": column_total,
                "pct_of_model": (column_total / total * 100) if measured and total else None,
            }
        )

    frame = pd.DataFrame.from_records(records, columns=list(COLUMNS))
    frame = frame.sort_values("total_bytes", ascending=False, na_position="last")
    return frame.reset_index(drop=True)


def table_summary(model: Model) -> pd.DataFrame:
    """One row per table: total bytes, column count, and share of the model."""
    import pandas as pd

    model.require("metrics")
    total = model.total_bytes

    records = []
    for table in model.tables.values():
        every = model.columns_of(table.name)
        user_columns = [c for c in every if not c.is_row_number]
        measured = [c for c in every if c.metrics]
        # Row-number storage is real storage, so it counts toward the table total and the
        # table totals therefore reconcile with model.total_bytes. It is broken out
        # separately because it is not a column anyone can act on.
        table_bytes = sum(c.metrics.total_bytes for c in measured)
        row_number_bytes = sum(c.metrics.total_bytes for c in measured if c.is_row_number)
        records.append(
            {
                "table": table.name,
                "hidden": table.is_hidden,
                "auto_date_time": table.is_auto_date_time,
                "rows": table.row_count,
                "columns": len(user_columns),
                "measured_columns": sum(1 for c in user_columns if c.metrics),
                "total_bytes": table_bytes,
                "row_number_bytes": row_number_bytes,
                "pct_of_model": (table_bytes / total * 100) if total else None,
            }
        )

    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        return frame
    return frame.sort_values("total_bytes", ascending=False).reset_index(drop=True)


def concentration(model: Model, top: int = 6) -> tuple[int, float]:
    """Bytes held by the ``top`` largest columns, and what share of the model that is.

    The headline number: a model is usually a handful of columns wearing a trenchcoat.
    """
    model.require("metrics")
    sizes = sorted(
        (
            c.metrics.total_bytes
            for c in model.columns.values()
            if c.metrics and not c.is_row_number
        ),
        reverse=True,
    )
    head = sum(sizes[:top])
    total = model.total_bytes
    return head, (head / total * 100) if total else 0.0


def _kind(column) -> str:  # noqa: ANN001 - Column, avoided to dodge a circular import
    if column.is_row_number:
        return "row_number"
    return "calculated" if column.is_calculated else "column"


__all__ = ["COLUMNS", "concentration", "table_summary", "vertipaq"]
