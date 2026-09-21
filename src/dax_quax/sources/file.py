"""Load a Model from a .pbix, .abf or PowerPivot .xlsx, with no Microsoft runtime.

The third source, and the one that needs nothing installed: `pbixray` reads the VertiPaq
store out of the file itself. So this is the only path that gives **metrics and metadata
together** on a machine with no Power BI Desktop — a live connection has no report layer, a
PBIP folder has no metrics without its cache, and a file has both.

    live        metadata expressions metrics dependencies execute
    pbip        metadata expressions                              report
    file        metadata expressions metrics

ON THE ACCURACY OF THIS FILE
----------------------------
pbixray's frames are read at run time and their exact column names have not been checked
against a real file. Rather than guess once, each logical field lists **candidate** column
names and the first one present wins; :func:`verify_contract` reports which alias matched,
or that none did, so one run against a real .pbix settles the whole thing.

A field that resolves to nothing is recorded in the model's warnings. It never becomes a
zero, because a column whose size failed to load and a column that is genuinely empty are
different facts.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

from dax_quax.errors import DaxQuaxError
from dax_quax.model import (
    Column,
    ColumnMetrics,
    Measure,
    Model,
    Relationship,
    Role,
    Table,
    TablePermission,
)

__all__ = [
    "FRAMES",
    "ContractReport",
    "apply_statistics",
    "build_model_from_source",
    "load_file",
    "verify_contract",
]

SUFFIXES = (".pbix", ".abf", ".xlsx")


@dataclass(frozen=True, slots=True)
class FrameSpec:
    """One pbixray property, and the columns this module needs out of it."""

    #: logical name -> candidate column names, in order of preference
    fields: dict[str, tuple[str, ...]]
    required: bool = False


#: The contract. Aliases exist because the frames are not documented column by column, and
#: a single wrong guess would silently drop a whole category of metadata.
FRAMES: dict[str, FrameSpec] = {
    "schema": FrameSpec(
        required=True,
        fields={
            "table": ("TableName", "Table", "table"),
            "column": ("ColumnName", "Column", "column"),
            "data_type": ("PandasDataType", "DataType", "Type"),
        },
    ),
    "statistics": FrameSpec(
        fields={
            "table": ("TableName", "Table"),
            "column": ("ColumnName", "Column"),
            "cardinality": ("Cardinality", "DistinctCount"),
            "dictionary": ("Dictionary", "DictionarySize"),
            "data": ("DataSize", "Data", "UsedSize"),
            "hierarchy": ("HierarchiesSize", "HierarchySize", "Hierarchies"),
            "encoding": ("Encoding", "ColumnEncoding"),
            "rows": ("RowCount", "Rows", "RecordCount"),
        },
    ),
    "dax_measures": FrameSpec(
        fields={
            "table": ("TableName", "Table"),
            "name": ("Name", "MeasureName"),
            "expression": ("Expression", "Measure"),
            "display_folder": ("DisplayFolder",),
            "description": ("Description",),
        },
    ),
    "dax_columns": FrameSpec(
        fields={
            "table": ("TableName", "Table"),
            "column": ("ColumnName", "Column"),
            "expression": ("Expression",),
        },
    ),
    "dax_tables": FrameSpec(
        fields={
            "table": ("TableName", "Table", "Name"),
            "expression": ("Expression",),
        },
    ),
    "relationships": FrameSpec(
        fields={
            "from_table": ("FromTableName", "FromTable"),
            "from_column": ("FromColumnName", "FromColumn"),
            "to_table": ("ToTableName", "ToTable"),
            "to_column": ("ToColumnName", "ToColumn"),
            "is_active": ("IsActive", "Active"),
            "cross_filter": ("CrossFilteringBehavior", "CrossFilter"),
        },
    ),
    "rls": FrameSpec(
        fields={
            "role": ("RoleName", "Role", "Name"),
            "table": ("TableName", "Table"),
            "expression": ("FilterExpression", "Expression"),
        },
    ),
}


@dataclass(slots=True)
class ContractReport:
    """Which alias matched for each declared field, or that none did."""

    resolved: dict[str, dict[str, str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    absent_frames: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.missing and not self.absent_frames


def _frame(source: Any, name: str) -> Any | None:
    """Read a pbixray property, tolerating one that raises or is absent."""
    try:
        frame = getattr(source, name, None)
    except Exception:  # noqa: BLE001 - a third-party parser over a binary format
        return None
    if frame is None:
        return None
    try:
        return frame if len(frame) else None
    except TypeError:
        return None


def _columns_of(frame: Any) -> list[str]:
    return [str(c) for c in getattr(frame, "columns", [])]


def _pick(frame: Any, candidates: tuple[str, ...]) -> str | None:
    present = _columns_of(frame)
    for candidate in candidates:
        if candidate in present:
            return candidate
    return None


def verify_contract(source: Any) -> ContractReport:
    """Check a real pbixray source against what this module claims to read.

    One run against an actual .pbix settles every alias in :data:`FRAMES`.
    """
    report = ContractReport()
    for name, spec in FRAMES.items():
        frame = _frame(source, name)
        if frame is None:
            if spec.required:
                report.absent_frames.append(name)
            continue
        resolved: dict[str, str] = {}
        for logical, candidates in spec.fields.items():
            found = _pick(frame, candidates)
            if found:
                resolved[logical] = found
            else:
                report.missing.append(f"{name}.{logical} (tried {', '.join(candidates)})")
        report.resolved[name] = resolved
    return report


def _rows(frame: Any) -> list[dict[str, Any]]:
    return frame.to_dict(orient="records")


def _get(row: dict[str, Any], names: dict[str, str], logical: str) -> Any:
    column = names.get(logical)
    return row.get(column) if column else None


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() != "nan" else None


def build_model_from_source(source: Any, *, name: str) -> Model:
    """Turn a pbixray source into a Model.

    Takes the object rather than a path so it can be tested against a stand-in carrying
    the same frames, with no binary file involved.
    """
    report = verify_contract(source)
    model = Model(name=name, source="file")

    if report.absent_frames:
        raise DaxQuaxError(
            f"{name}: this file has no {', '.join(report.absent_frames)} — it may have no "
            "embedded model (a live-connection report, say) rather than being unreadable"
        )
    if report.missing:
        model.warnings.append(
            f"{len(report.missing)} field(s) of the pbixray contract did not resolve, so "
            "that metadata is missing rather than empty: " + "; ".join(report.missing[:6])
        )

    names = report.resolved
    _build_tables_and_columns(source, model, names)
    _apply_expressions(source, model, names)
    _apply_measures(source, model, names)
    _apply_metrics(source, model, names)
    _apply_relationships(source, model, names)
    _apply_roles(source, model, names)

    capabilities = {"metadata", "expressions"}
    # pbixray reads DAX out of a different part of the file than the schema, and on some
    # .pbix files it returns nothing at all -- Microsoft's own "Adventure Works DW 2020"
    # sample, which has plenty of measures, comes back with zero. A model that claims the
    # `expressions` capability while holding no expressions is the same lie as claiming
    # `metrics` with every size 0: nothing references anything, so every column looks
    # unused. Today the scope degrade saves it, because a .pbix carries no report layer --
    # but `--pbix` with `--report-folder` makes the scope trustworthy again and the
    # verdicts real.
    has_dax = model.measures or any(c.is_calculated for c in model.columns.values())
    if model.tables and not has_dax:
        model.warnings.append(
            "no DAX was read from this file: no measures, no calculated columns, no "
            "calculated tables. Either this model genuinely has none, or pbixray could "
            "not read them -- the two look identical from here. Nothing references "
            "anything, so do not trust an unreferenced verdict against this model."
        )
    if any(column.metrics for column in model.columns.values()):
        capabilities.add("metrics")
    else:
        model.warnings.append(
            "no storage statistics were read from this file, so there are no sizes. A .pbix "
            "saved without data, or one whose statistics frame did not resolve, looks the same."
        )
    model.capabilities = frozenset(capabilities)
    return model


def _build_tables_and_columns(source: Any, model: Model, names: dict) -> None:
    frame = _frame(source, "schema")
    for row in _rows(frame) if frame is not None else []:
        table = _text(_get(row, names.get("schema", {}), "table"))
        column = _text(_get(row, names.get("schema", {}), "column"))
        if not table:
            continue
        model.tables.setdefault(table, Table(name=table))
        if not column:
            continue
        obj = Column(
            table=table,
            name=column,
            data_type=_text(_get(row, names.get("schema", {}), "data_type")),
        )
        model.columns.setdefault(obj.key, obj)


def _apply_expressions(source: Any, model: Model, names: dict) -> None:
    frame = _frame(source, "dax_columns")
    for row in _rows(frame) if frame is not None else []:
        mapping = names.get("dax_columns", {})
        table = _text(_get(row, mapping, "table"))
        column = _text(_get(row, mapping, "column"))
        expression = _text(_get(row, mapping, "expression"))
        target = model.columns.get(f"{table}[{column}]")
        if target is not None and expression:
            target.is_calculated = True
            target.expression = expression

    frame = _frame(source, "dax_tables")
    for row in _rows(frame) if frame is not None else []:
        mapping = names.get("dax_tables", {})
        table = _text(_get(row, mapping, "table"))
        expression = _text(_get(row, mapping, "expression"))
        target = model.tables.get(table or "")
        if target is not None and expression:
            target.is_calculated = True
            target.source = expression


def _apply_measures(source: Any, model: Model, names: dict) -> None:
    frame = _frame(source, "dax_measures")
    mapping = names.get("dax_measures", {})
    for row in _rows(frame) if frame is not None else []:
        table = _text(_get(row, mapping, "table"))
        measure_name = _text(_get(row, mapping, "name"))
        if not table or not measure_name:
            continue
        measure = Measure(
            table=table,
            name=measure_name,
            expression=_text(_get(row, mapping, "expression")),
            display_folder=_text(_get(row, mapping, "display_folder")),
            description=_text(_get(row, mapping, "description")),
        )
        model.measures[measure.key] = measure


def apply_statistics(source: Any, model: Model, names: dict | None = None) -> int:
    """Attach pbixray storage statistics to a Model's columns, and say how many matched.

    Public because the .pbip loader reads the same statistics out of a project's local
    cache.abf. One contract for pbixray, not two that drift.
    """
    names = names if names is not None else verify_contract(source).resolved
    return _apply_metrics(source, model, names)


def _apply_metrics(source: Any, model: Model, names: dict) -> int:
    frame = _frame(source, "statistics")
    mapping = names.get("statistics", {})
    matched = 0
    for row in _rows(frame) if frame is not None else []:
        table = _text(_get(row, mapping, "table"))
        column = _text(_get(row, mapping, "column"))
        target = model.columns.get(f"{table}[{column}]")
        if target is None:
            continue
        matched += 1
        target.metrics = ColumnMetrics(
            dictionary_bytes=_int(_get(row, mapping, "dictionary")) or 0,
            data_bytes=_int(_get(row, mapping, "data")) or 0,
            hierarchy_bytes=_int(_get(row, mapping, "hierarchy")) or 0,
            cardinality=_int(_get(row, mapping, "cardinality")),
            encoding=_text(_get(row, mapping, "encoding")),
            rows=_int(_get(row, mapping, "rows")),
        )
    if frame is not None and matched == 0 and model.columns:
        model.warnings.append(
            "storage statistics were read but none matched a column of the schema, so the "
            "two frames disagree about table or column naming; sizes are unavailable"
        )
    return matched


def _apply_relationships(source: Any, model: Model, names: dict) -> None:
    frame = _frame(source, "relationships")
    mapping = names.get("relationships", {})
    for row in _rows(frame) if frame is not None else []:
        from_table = _text(_get(row, mapping, "from_table"))
        from_column = _text(_get(row, mapping, "from_column"))
        to_table = _text(_get(row, mapping, "to_table"))
        to_column = _text(_get(row, mapping, "to_column"))
        if not (from_table and from_column and to_table and to_column):
            model.warnings.append(f"relationship with unreadable endpoints skipped: {row!r}")
            continue
        active = _get(row, mapping, "is_active")
        model.relationships.append(
            Relationship(
                from_table=from_table,
                from_column=from_column,
                to_table=to_table,
                to_column=to_column,
                is_active=True if active is None else bool(active),
                cross_filter=_text(_get(row, mapping, "cross_filter")),
            )
        )


def _apply_roles(source: Any, model: Model, names: dict) -> None:
    frame = _frame(source, "rls")
    mapping = names.get("rls", {})
    by_name: dict[str, Role] = {}
    for row in _rows(frame) if frame is not None else []:
        role_name = _text(_get(row, mapping, "role")) or "(unnamed role)"
        table = _text(_get(row, mapping, "table"))
        if not table:
            continue
        role = by_name.get(role_name)
        if role is None:
            role = Role(name=role_name)
            by_name[role_name] = role
            model.roles.append(role)
        role.table_permissions.append(
            TablePermission(
                table=table,
                role=role_name,
                filter_expression=_text(_get(row, mapping, "expression")),
            )
        )


def load_file(path: str | pathlib.Path) -> Model:
    """Load a .pbix, .abf or PowerPivot .xlsx. Needs the ``file`` extra."""
    path = pathlib.Path(path)
    if not path.is_file():
        raise DaxQuaxError(f"not a file: {path}")
    if path.suffix.lower() not in SUFFIXES:
        raise DaxQuaxError(
            f"{path.name}: expected one of {', '.join(SUFFIXES)}. A .pbip project is a "
            "folder, not a file — use open_pbip()."
        )
    try:
        from pbixray import PBIXRay
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DaxQuaxError(
            "reading a .pbix needs pbixray: pip install dax-quax[file]"
        ) from exc

    try:
        source = PBIXRay(str(path))
    except Exception as exc:  # noqa: BLE001 - third-party parser over a binary format
        raise DaxQuaxError(f"{path.name} could not be read: {type(exc).__name__}: {exc}") from exc

    try:
        return build_model_from_source(source, name=path.stem)
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()
