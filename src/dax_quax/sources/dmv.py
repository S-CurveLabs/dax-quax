"""The DMV contract, and the pure transformation from rowsets to a Model.

Nothing here touches a connection. ``live.py`` executes the statements below and hands the
rowsets to :func:`build_model`, which means the whole transformation is testable from
recorded JSON with no Power BI Desktop present.

ON THE ACCURACY OF THIS FILE
----------------------------
The statements are ``SELECT *`` on purpose: a renamed or missing column then shows up as a
contract finding rather than a query failure. Every column this module reads is declared in
``columns``, and every *join* whose shape has not yet been confirmed against a real engine is
registered in :data:`UNVERIFIED_JOINS`. Each such join also emits a model warning when it
matches zero rows, so a wrong guess is loud.

Run ``verify_contract()`` against a live connection to settle all of it at once.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from dax_quax.model import (
    CalculationItem,
    Column,
    ColumnMetrics,
    Hierarchy,
    Level,
    Measure,
    Model,
    Relationship,
    Role,
    Table,
    TablePermission,
)

Row = dict[str, Any]
Rowsets = dict[str, list[Row]]


@dataclass(frozen=True, slots=True)
class DmvQuery:
    name: str
    statement: str
    columns: tuple[str, ...]
    optional: tuple[str, ...] = ()
    required: bool = True


def _q(name: str, dmv: str, columns: str, optional: str = "", required: bool = True) -> DmvQuery:
    return DmvQuery(
        name=name,
        statement=f"SELECT * FROM $SYSTEM.{dmv}",
        columns=tuple(columns.split()),
        optional=tuple(optional.split()),
        required=required,
    )


# -- metadata -------------------------------------------------------------------------

QUERIES: dict[str, DmvQuery] = {
    "tables": _q("tables", "TMSCHEMA_TABLES", "ID Name IsHidden"),
    "columns": _q(
        "columns",
        "TMSCHEMA_COLUMNS",
        "ID TableID Type",
        optional="ExplicitName InferredName ExplicitDataType InferredDataType IsHidden "
        "Expression SortByColumnID DisplayFolder",
    ),
    "measures": _q(
        "measures",
        "TMSCHEMA_MEASURES",
        "ID TableID Name",
        optional="Expression IsHidden DisplayFolder FormatString FormatStringDefinition "
        "Description",
    ),
    "relationships": _q(
        "relationships",
        "TMSCHEMA_RELATIONSHIPS",
        "FromTableID FromColumnID ToTableID ToColumnID",
        optional="IsActive CrossFilteringBehavior FromCardinality ToCardinality",
    ),
    "partitions": _q(
        "partitions",
        "TMSCHEMA_PARTITIONS",
        "TableID",
        optional="Mode Type QueryDefinition",
        required=False,
    ),
    "roles": _q(
        "roles", "TMSCHEMA_ROLES", "ID Name", optional="ModelPermission", required=False
    ),
    "table_permissions": _q(
        "table_permissions",
        "TMSCHEMA_TABLE_PERMISSIONS",
        "RoleID TableID",
        optional="FilterExpression",
        required=False,
    ),
    "hierarchies": _q(
        "hierarchies",
        "TMSCHEMA_HIERARCHIES",
        "ID TableID Name",
        optional="IsHidden",
        required=False,
    ),
    "levels": _q(
        "levels",
        "TMSCHEMA_LEVELS",
        "HierarchyID ColumnID Name",
        optional="Ordinal",
        required=False,
    ),
    # UNVERIFIED: these two have never been run against a real engine, so they are
    # `required=False` and a failure leaves the group-by edge missing rather than the scan
    # broken. The disk sources read the same relationship from `relatedColumnDetails`.
    "related_column_details": _q(
        "related_column_details",
        "TMSCHEMA_RELATED_COLUMN_DETAILS",
        "ID ColumnID",
        required=False,
    ),
    "group_by_columns": _q(
        "group_by_columns",
        "TMSCHEMA_GROUP_BY_COLUMNS",
        "RelatedColumnDetailsID GroupingColumnID",
        required=False,
    ),
    "calculation_groups": _q(
        "calculation_groups", "TMSCHEMA_CALCULATION_GROUPS", "ID TableID", required=False
    ),
    "calculation_items": _q(
        "calculation_items",
        "TMSCHEMA_CALCULATION_ITEMS",
        "CalculationGroupID Name",
        optional="Expression FormatStringDefinition Ordinal",
        required=False,
    ),
    "detail_rows": _q(
        "detail_rows",
        "TMSCHEMA_DETAIL_ROWS_DEFINITIONS",
        "Expression",
        optional="MeasureID TableID ObjectType ObjectID",
        required=False,
    ),
    "kpis": _q(
        "kpis",
        "TMSCHEMA_KPIS",
        "MeasureID",
        optional="TargetExpression StatusExpression TrendExpression",
        required=False,
    ),
    "calc_dependency": _q(
        "calc_dependency",
        "DISCOVER_CALC_DEPENDENCY",
        "OBJECT_TYPE TABLE OBJECT REFERENCED_OBJECT_TYPE REFERENCED_TABLE REFERENCED_OBJECT",
        optional="EXPRESSION REFERENCED_EXPRESSION",
        required=False,
    ),
    # -- metrics ----------------------------------------------------------------------
    "storage_tables": _q(
        "storage_tables",
        "DISCOVER_STORAGE_TABLES",
        "DIMENSION_NAME TABLE_ID ROWS_COUNT",
        required=False,
    ),
    "storage_columns": _q(
        "storage_columns",
        "DISCOVER_STORAGE_TABLE_COLUMNS",
        "DIMENSION_NAME COLUMN_ID",
        optional="DICTIONARY_SIZE COLUMN_ENCODING COLUMN_TYPE",
        required=False,
    ),
    "segments": _q(
        "segments",
        "DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS",
        "DIMENSION_NAME TABLE_ID COLUMN_ID USED_SIZE",
        optional="RECORDS_COUNT SEGMENT_NUMBER COMPRESSION_TYPE",
        required=False,
    ),
}

METADATA_QUERIES = (
    "tables", "columns", "measures", "relationships", "partitions", "calc_dependency",
    "roles", "table_permissions", "hierarchies", "levels", "calculation_groups",
    "calculation_items", "detail_rows", "kpis", "related_column_details",
    "group_by_columns",
)
METRIC_QUERIES = ("storage_tables", "storage_columns", "segments")

#: Joins whose shape has not been confirmed against a real engine. Settle these first.
#:
#: Verified against Power BI Desktop 2.157.1354.0 on 2026-09-18, and all three were wrong.
#: The engine suffixes an internal id onto the identifiers these joined on -- TABLE_ID is
#: "Calendar (10)" and COLUMN_ID is "Day (49)", not "Calendar" and "Day" -- so every one
#: of them matched nothing. The model claimed the `metrics` capability with every size
#: zero, which is precisely the "0 B is a lie" failure this project exists to avoid.
UNVERIFIED_JOINS: dict[str, str] = {}

#: What the joins actually are, now that a real engine has been asked.
VERIFIED_JOINS: dict[str, str] = {
    "column_storage": (
        "storage rows are matched to model columns on (DIMENSION_NAME, ATTRIBUTE_NAME). "
        "COLUMN_ID carries an internal id suffix, ATTRIBUTE_NAME does not. Only "
        "COLUMN_TYPE == 'BASIC_DATA' rows are user columns: the hierarchy and "
        "relationship rows reuse a neighbouring column's ATTRIBUTE_NAME."
    ),
    "segment_bytes": (
        "segment rows carry COLUMN_ID but no ATTRIBUTE_NAME, so the id suffix is stripped "
        "from COLUMN_ID to reach the column name."
    ),
    "table_rows": (
        "a table's row count is the DISCOVER_STORAGE_TABLES row whose TABLE_ID, with its "
        "id suffix stripped, equals DIMENSION_NAME."
    ),
    "hierarchy_bytes": (
        "per-column hierarchy size is summed from segment rows whose TABLE_ID matches "
        "'H$<table>$<column>', both parts carrying an id suffix to strip."
    ),
}

#: Column cardinality is NOT available from these DMVs on this engine. The '$<table>$
#: <column>' dictionary pseudo-tables the old shape assumed do not exist; DISCOVER_STORAGE_
#: TABLES holds only the tables themselves and the H$/U$/R$ hierarchy and relationship
#: structures. The H$ row's ROWS_COUNT sits a consistent three above the distinct count in
#: every column sampled, and a magic constant fitted to one model is not a measurement, so
#: cardinality stays None rather than becoming a number nobody can defend.
CARDINALITY_UNAVAILABLE = (
    "column cardinality is not in these DMV rowsets; it stays None rather than being "
    "inferred from hierarchy sizes"
)

_HIER_TABLE_RE = re.compile(r"^H\$(?P<table>.+?)\$(?P<column>.+)$")
#: The engine writes identifiers as "Calendar (10)" and "Day (49)". The suffix is an
#: internal id, not part of the name, and joining on the unstripped form matches nothing.
_ID_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def strip_id(text: str | None) -> str:
    """'Calendar (10)' -> 'Calendar'. Safe on a name that carries no suffix."""
    return _ID_SUFFIX_RE.sub("", text or "")

# TMSCHEMA_COLUMNS.Type
_COL_TYPE_DATA, _COL_TYPE_CALCULATED, _COL_TYPE_ROWNUMBER, _COL_TYPE_CALC_TABLE = 1, 2, 3, 4

_DATA_TYPES = {
    2: "String", 6: "Int64", 8: "Double", 9: "DateTime",
    10: "Decimal", 11: "Boolean", 17: "Binary", 19: "Variant",
}


# -- helpers --------------------------------------------------------------------------


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# -- contract verification -------------------------------------------------------------


@dataclass(slots=True)
class ContractFinding:
    query: str
    kind: str  # "missing-required" | "missing-optional" | "empty" | "absent"
    detail: str


def verify_contract(rowsets: Rowsets) -> list[ContractFinding]:
    """Check returned rowsets against what this module claims to read.

    Run it once against a real engine and every guess in this file is settled.
    """
    findings: list[ContractFinding] = []
    for name, query in QUERIES.items():
        rows = rowsets.get(name)
        if rows is None:
            findings.append(ContractFinding(name, "absent", "query was not run"))
            continue
        if not rows:
            findings.append(ContractFinding(name, "empty", "query returned no rows"))
            continue
        present = set(rows[0])
        for column in query.columns:
            if column not in present:
                findings.append(
                    ContractFinding(
                        name, "missing-required", f"{column!r} not in {sorted(present)}"
                    )
                )
        for column in query.optional:
            if column not in present:
                findings.append(ContractFinding(name, "missing-optional", f"{column!r} absent"))
    return findings


# -- the transformation ----------------------------------------------------------------


@dataclass(slots=True)
class _Build:
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def build_model(
    rowsets: Rowsets,
    *,
    name: str,
    source: str = "live",
    capabilities: frozenset[str] | None = None,
) -> Model:
    """Turn DMV rowsets into a Model. Pure — no I/O, no connection."""
    b = _Build()

    tables, table_by_id = _build_tables(rowsets, b)
    columns, column_by_id, sort_by_ids = _build_columns(rowsets, table_by_id, b)
    _resolve_sort_by(columns, column_by_id, sort_by_ids, b)
    measures = _build_measures(rowsets, table_by_id, b)
    relationships = _build_relationships(rowsets, table_by_id, column_by_id, b)
    _apply_partitions(rowsets, tables, table_by_id)
    roles = _build_roles(rowsets, table_by_id, b)
    hierarchies = _build_hierarchies(rowsets, table_by_id, column_by_id, b)
    _apply_group_by(rowsets, columns, column_by_id, b)
    items = _build_calculation_items(rowsets, table_by_id, b)
    _apply_detail_rows(rowsets, measures, b)
    _apply_kpis(rowsets, measures)

    has_metrics = any(rowsets.get(q) for q in METRIC_QUERIES)
    if has_metrics:
        _apply_metrics(rowsets, tables, columns, b)

    if capabilities is None:
        capabilities = frozenset(
            {"metadata", "expressions"}
            | ({"metrics"} if has_metrics else set())
            | ({"dependencies", "execute"} if source == "live" else set())
        )

    if not tables:
        # A scan that finds nothing must say so. Power BI Desktop with a blank report
        # serves a real, empty database: every DMV answers, none of them with rows, and
        # without this the result is a Model claiming the `metadata` capability and
        # holding no metadata -- indistinguishable from a model whose objects we failed
        # to read. The engine is blunter than we were: "The database is empty."
        b.warnings.append(
            "this database has no tables. If Power BI Desktop is open on a blank report "
            "that is correct; otherwise the metadata queries returned nothing and the "
            "scan below is empty rather than clean."
        )

    return Model(
        name=name,
        source=source,
        capabilities=capabilities,
        tables=tables,
        columns=columns,
        measures=measures,
        relationships=relationships,
        roles=roles,
        hierarchies=hierarchies,
        calculation_items=items,
        warnings=b.warnings,
    )


def _build_tables(rowsets: Rowsets, b: _Build) -> tuple[dict[str, Table], dict[int, str]]:
    tables: dict[str, Table] = {}
    by_id: dict[int, str] = {}
    for row in rowsets.get("tables", []):
        table_name = _str(row.get("Name"))
        table_id = _int(row.get("ID"))
        if not table_name or table_id is None:
            b.warn(f"skipped a TMSCHEMA_TABLES row with no usable ID/Name: {row!r}")
            continue
        tables[table_name] = Table(name=table_name, is_hidden=_bool(row.get("IsHidden")))
        by_id[table_id] = table_name
    return tables, by_id


def _build_columns(
    rowsets: Rowsets, table_by_id: dict[int, str], b: _Build
) -> tuple[dict[str, Column], dict[int, str], dict[str, int]]:
    columns: dict[str, Column] = {}
    by_id: dict[int, str] = {}
    sort_by_ids: dict[str, int] = {}
    for row in rowsets.get("columns", []):
        table_id = _int(row.get("TableID"))
        table_name = table_by_id.get(table_id) if table_id is not None else None
        if table_name is None:
            b.warn(f"column with unknown TableID={row.get('TableID')!r} skipped")
            continue
        col_name = _str(row.get("ExplicitName")) or _str(row.get("InferredName"))
        col_type = _int(row.get("Type"), _COL_TYPE_DATA)
        if not col_name:
            if col_type != _COL_TYPE_ROWNUMBER:
                b.warn(f"column in {table_name!r} has no ExplicitName or InferredName; skipped")
            continue
        data_type_code = _int(row.get("ExplicitDataType")) or _int(row.get("InferredDataType"))
        column = Column(
            table=table_name,
            name=col_name,
            data_type=_DATA_TYPES.get(data_type_code) if data_type_code else None,
            is_hidden=_bool(row.get("IsHidden")),
            is_calculated=col_type in (_COL_TYPE_CALCULATED, _COL_TYPE_CALC_TABLE),
            is_row_number=col_type == _COL_TYPE_ROWNUMBER,
            expression=_str(row.get("Expression")),
            display_folder=_str(row.get("DisplayFolder")),
        )
        columns[column.key] = column
        col_id = _int(row.get("ID"))
        if col_id is not None:
            by_id[col_id] = column.key
        sort_by_id = _int(row.get("SortByColumnID"))
        if sort_by_id is not None:
            sort_by_ids[column.key] = sort_by_id
    return columns, by_id, sort_by_ids


def _resolve_sort_by(
    columns: dict[str, Column],
    column_by_id: dict[int, str],
    sort_by_ids: dict[str, int],
    b: _Build,
) -> None:
    """Second pass: SortByColumnID may point at a column not yet seen during the first.

    This is a real usage edge. A column referenced only as another column's sort order is
    still in use, and dropping the edge produces a false 'unused' verdict in M4.
    """
    for column_key, target_id in sort_by_ids.items():
        target_key = column_by_id.get(target_id)
        if target_key is None:
            b.warn(f"{column_key} sorts by unknown column id {target_id}; edge lost")
            continue
        columns[column_key].sort_by = target_key


def _apply_group_by(
    rowsets: Rowsets,
    columns: dict[str, Column],
    column_by_id: dict[int, str],
    b: _Build,
) -> None:
    """Join GROUP_BY_COLUMNS to its owning column, via RELATED_COLUMN_DETAILS.

    The disk sources read this from `relatedColumnDetails`; without the same edge here, a
    field parameter's hidden grouping column reports REMOVE on a live scan but not on a
    PBIP one. UNVERIFIED against a real engine — both rowsets are optional, so a model
    that returns neither simply keeps the edge it always lacked.
    """
    owner_by_details: dict[int, str] = {}
    for row in rowsets.get("related_column_details", []):
        details_id = _int(row.get("ID"))
        owner = column_by_id.get(_int(row.get("ColumnID")))
        if details_id is not None and owner:
            owner_by_details[details_id] = owner

    for row in rowsets.get("group_by_columns", []):
        owner = owner_by_details.get(_int(row.get("RelatedColumnDetailsID")))
        target = column_by_id.get(_int(row.get("GroupingColumnID")))
        if owner is None or target is None:
            b.warn(f"group-by column with an unresolvable endpoint skipped: {row!r}")
            continue
        if owner in columns and columns[owner].group_by is None:
            columns[owner].group_by = target


def _build_measures(rowsets: Rowsets, table_by_id: dict[int, str], b: _Build) -> dict[str, Measure]:
    measures: dict[str, Measure] = {}
    for row in rowsets.get("measures", []):
        table_id = _int(row.get("TableID"))
        table_name = table_by_id.get(table_id) if table_id is not None else None
        measure_name = _str(row.get("Name"))
        if table_name is None or not measure_name:
            b.warn(f"measure {row.get('Name')!r} has unknown TableID; skipped")
            continue
        measure = Measure(
            table=table_name,
            name=measure_name,
            expression=_str(row.get("Expression")),
            is_hidden=_bool(row.get("IsHidden")),
            display_folder=_str(row.get("DisplayFolder")),
            format_string=_str(row.get("FormatString")),
            format_expression=_str(row.get("FormatStringDefinition")),
            description=_str(row.get("Description")),
        )
        measures[measure.key] = measure
    return measures


def _build_relationships(
    rowsets: Rowsets, table_by_id: dict[int, str], column_by_id: dict[int, str], b: _Build
) -> list[Relationship]:
    out: list[Relationship] = []
    for row in rowsets.get("relationships", []):
        from_table = table_by_id.get(_int(row.get("FromTableID")))
        to_table = table_by_id.get(_int(row.get("ToTableID")))
        from_key = column_by_id.get(_int(row.get("FromColumnID")))
        to_key = column_by_id.get(_int(row.get("ToColumnID")))
        if not (from_table and to_table and from_key and to_key):
            b.warn(f"relationship with unresolvable endpoints skipped: {row!r}")
            continue
        out.append(
            Relationship(
                from_table=from_table,
                from_column=from_key.split("[", 1)[1].rstrip("]"),
                to_table=to_table,
                to_column=to_key.split("[", 1)[1].rstrip("]"),
                is_active=_bool(row.get("IsActive"), default=True),
                cross_filter=_str(row.get("CrossFilteringBehavior")),
                from_cardinality=_str(row.get("FromCardinality")),
                to_cardinality=_str(row.get("ToCardinality")),
            )
        )
    return out


#: The only DISCOVER_STORAGE_TABLE_COLUMNS rows that are user columns.
_BASIC_DATA = "BASIC_DATA"

#: TMSCHEMA_PARTITIONS.Type, as TOM's PartitionSourceType enum. Verified against Power BI
#: Desktop 2.157.1354.0 on 2026-09-18, and the previous value was exactly inverted: 4 was
#: declared to mean "calculated" and in fact means M. The consequences ran both ways --
#: every Power Query table was walked as if its `let ... in` were DAX, inventing
#: references, and every real DAX table went unwalked, so a field parameter's NAMEOF
#: dependencies were invisible and two objects it keeps alive reported REMOVE.
_PARTITION_TYPE_QUERY = 1
_PARTITION_TYPE_CALCULATED = 2
_PARTITION_TYPE_M = 4
_PARTITION_TYPE_CALCULATION_GROUP = 7

#: TMSCHEMA_PARTITIONS.Mode arrives as a number. PBIP writes the word, and a Model whose
#: `mode` reads "0" from one source and "import" from the other cannot be compared.
_PARTITION_MODES = {
    0: "import",
    1: "directQuery",
    2: "default",
    3: "dual",
    4: "push",
    5: "streaming",
    6: "pushStreaming",
}


def _apply_partitions(
    rowsets: Rowsets, tables: dict[str, Table], table_by_id: dict[int, str]
) -> None:
    for row in rowsets.get("partitions", []):
        table_name = table_by_id.get(_int(row.get("TableID")))
        if not table_name or table_name not in tables:
            continue
        table = tables[table_name]
        mode = _int(row.get("Mode"))
        if mode is not None:
            table.mode = _PARTITION_MODES.get(mode, str(mode))
        elif _str(row.get("Mode")):
            table.mode = _str(row.get("Mode"))
        # The source text matters because it is where a calculated table's DAX lives, and
        # where a model records that it reads another model.
        table.source = _str(row.get("QueryDefinition")) or table.source
        # Only Type 2 is DAX. Feeding a Type 4 partition's M to the DAX extractor does not
        # fail loudly -- it finds plausible-looking references and invents edges.
        if _int(row.get("Type")) == _PARTITION_TYPE_CALCULATED:
            table.is_calculated = True


def _build_roles(rowsets: Rowsets, table_by_id: dict[int, str], b: _Build) -> list[Role]:
    out: list[Role] = []
    by_id: dict[int, Role] = {}
    for row in rowsets.get("roles", []):
        role_id = _int(row.get("ID"))
        name = _str(row.get("Name"))
        if role_id is None or not name:
            continue
        role = Role(name=name, model_permission=_str(row.get("ModelPermission")))
        by_id[role_id] = role
        out.append(role)

    for row in rowsets.get("table_permissions", []):
        role = by_id.get(_int(row.get("RoleID")))
        table_name = table_by_id.get(_int(row.get("TableID")))
        if role is None or table_name is None:
            b.warn(f"table permission with an unresolvable role or table skipped: {row!r}")
            continue
        role.table_permissions.append(
            TablePermission(
                table=table_name,
                role=role.name,
                filter_expression=_str(row.get("FilterExpression")),
            )
        )
    return out


def _build_hierarchies(
    rowsets: Rowsets,
    table_by_id: dict[int, str],
    column_by_id: dict[int, str],
    b: _Build,
) -> list[Hierarchy]:
    out: list[Hierarchy] = []
    by_id: dict[int, Hierarchy] = {}
    for row in rowsets.get("hierarchies", []):
        hierarchy_id = _int(row.get("ID"))
        table_name = table_by_id.get(_int(row.get("TableID")))
        name = _str(row.get("Name"))
        if hierarchy_id is None or table_name is None or not name:
            b.warn(f"hierarchy with an unresolvable table skipped: {row!r}")
            continue
        hierarchy = Hierarchy(table=table_name, name=name, is_hidden=_bool(row.get("IsHidden")))
        by_id[hierarchy_id] = hierarchy
        out.append(hierarchy)

    for row in rowsets.get("levels", []):
        hierarchy = by_id.get(_int(row.get("HierarchyID")))
        if hierarchy is None:
            b.warn(f"hierarchy level with an unresolvable hierarchy skipped: {row!r}")
            continue
        column_key = column_by_id.get(_int(row.get("ColumnID")))
        hierarchy.levels.append(
            Level(
                name=_str(row.get("Name")) or "",
                # The DMV gives a column id; the Model wants the column's own name.
                column=column_key.split("[", 1)[1].rstrip("]") if column_key else None,
                ordinal=_int(row.get("Ordinal")),
            )
        )
    return out


def _build_calculation_items(
    rowsets: Rowsets, table_by_id: dict[int, str], b: _Build
) -> list[CalculationItem]:
    group_table: dict[int, str] = {}
    for row in rowsets.get("calculation_groups", []):
        group_id = _int(row.get("ID"))
        table_name = table_by_id.get(_int(row.get("TableID")))
        if group_id is not None and table_name:
            group_table[group_id] = table_name

    out: list[CalculationItem] = []
    for row in rowsets.get("calculation_items", []):
        table_name = group_table.get(_int(row.get("CalculationGroupID")))
        name = _str(row.get("Name"))
        if table_name is None or not name:
            b.warn(f"calculation item with an unresolvable group skipped: {row!r}")
            continue
        out.append(
            CalculationItem(
                table=table_name,
                name=name,
                expression=_str(row.get("Expression")),
                format_expression=_str(row.get("FormatStringDefinition")),
                ordinal=_int(row.get("Ordinal")),
            )
        )
    return out


def _apply_kpis(rowsets: Rowsets, measures: dict[str, Measure]) -> None:
    """Attach a KPI's target, status and trend DAX to the measure that owns it.

    Without this a live scan and a PBIP scan of the same model disagree: TMDL carries the
    `kpi` block inside the measure and the DMV keeps it in its own rowset, so only the
    disk side saw it. The equivalence check in CONVENTIONS section 10 is what surfaced it.
    """
    by_id: dict[int, Measure] = {}
    for row in rowsets.get("measures", []):
        name = _str(row.get("Name"))
        measure_id = _int(row.get("ID"))
        if name and measure_id is not None and f"[{name}]" in measures:
            by_id[measure_id] = measures[f"[{name}]"]

    for row in rowsets.get("kpis", []):
        owner = by_id.get(_int(row.get("MeasureID")))
        if owner is None:
            continue
        found = (
            _str(row.get("TargetExpression")),
            _str(row.get("StatusExpression")),
            _str(row.get("TrendExpression")),
        )
        owner.kpi_expressions = tuple(expression for expression in found if expression)


def _apply_detail_rows(
    rowsets: Rowsets, measures: dict[str, Measure], b: _Build
) -> None:
    """Attach detail rows expressions to their measure.

    The DMV names the owner inconsistently across versions - MeasureID on some, an
    ObjectType/ObjectID pair on others - so both are tried, and anything still unattached
    is reported rather than dropped.
    """
    ids: dict[int, Measure] = {}
    for row in rowsets.get("measures", []):
        name = _str(row.get("Name"))
        measure_id = _int(row.get("ID"))
        if name and measure_id is not None and f"[{name}]" in measures:
            ids[measure_id] = measures[f"[{name}]"]

    for row in rowsets.get("detail_rows", []):
        expression = _str(row.get("Expression"))
        if not expression:
            continue
        owner = ids.get(_int(row.get("MeasureID")))
        if owner is None and _str(row.get("ObjectType")) in {"Measure", "3"}:
            owner = ids.get(_int(row.get("ObjectID")))
        if owner is None:
            b.warn(
                "a detail rows expression could not be tied to a measure, so the columns "
                f"it references may look unreferenced: {expression[:60]!r}"
            )
            continue
        owner.detail_rows = expression


def _apply_metrics(
    rowsets: Rowsets, tables: dict[str, Table], columns: dict[str, Column], b: _Build
) -> None:
    data_bytes: dict[tuple[str, str], int] = defaultdict(int)
    rows_count: dict[tuple[str, str], int] = defaultdict(int)
    hier_bytes: dict[tuple[str, str], int] = defaultdict(int)

    for row in rowsets.get("segments", []):
        used = _int(row.get("USED_SIZE"), 0) or 0
        table_id = _str(row.get("TABLE_ID")) or ""
        hier = _HIER_TABLE_RE.match(table_id)
        if hier:
            hier_bytes[(strip_id(hier["table"]), strip_id(hier["column"]))] += used
            continue
        dimension = _str(row.get("DIMENSION_NAME"))
        # Segment rows carry no ATTRIBUTE_NAME, so the id suffix comes off COLUMN_ID.
        column_name = strip_id(_str(row.get("COLUMN_ID")))
        if not dimension or not column_name:
            continue
        data_bytes[(dimension, column_name)] += used
        rows_count[(dimension, column_name)] += _int(row.get("RECORDS_COUNT"), 0) or 0

    dictionary: dict[tuple[str, str], int] = {}
    encoding: dict[tuple[str, str], str | None] = {}
    for row in rowsets.get("storage_columns", []):
        # Only BASIC_DATA rows are user columns. The hierarchy and relationship rows reuse
        # a neighbouring column's ATTRIBUTE_NAME, so including them overwrites real sizes.
        if _str(row.get("COLUMN_TYPE")) != _BASIC_DATA:
            continue
        dimension = _str(row.get("DIMENSION_NAME"))
        column_name = _str(row.get("ATTRIBUTE_NAME"))
        if not dimension or not column_name:
            continue
        dictionary[(dimension, column_name)] = _int(row.get("DICTIONARY_SIZE"), 0) or 0
        encoding[(dimension, column_name)] = _str(row.get("COLUMN_ENCODING"))

    for row in rowsets.get("storage_tables", []):
        dimension = _str(row.get("DIMENSION_NAME"))
        if dimension and dimension in tables and strip_id(_str(row.get("TABLE_ID"))) == dimension:
            tables[dimension].row_count = _int(row.get("ROWS_COUNT"))

    matched = 0
    for column in columns.values():
        pair = (column.table, column.name)
        seen = pair in data_bytes or pair in dictionary
        if not seen:
            continue
        matched += 1
        column.metrics = ColumnMetrics(
            dictionary_bytes=dictionary.get(pair, 0),
            data_bytes=data_bytes.get(pair, 0),
            hierarchy_bytes=hier_bytes.get(pair, 0),
            # See CARDINALITY_UNAVAILABLE: these rowsets do not carry it.
            cardinality=None,
            encoding=encoding.get(pair),
            rows=rows_count.get(pair),
        )

    # These joins are settled against one engine build, not proven for every future one.
    # A zero match used to mean "the guess was wrong"; it now means "the shape changed",
    # which is the same silent 0 B and deserves the same noise.
    real_columns = [c for c in columns.values() if not c.is_row_number]
    if real_columns and matched == 0:
        b.warn(
            "JOIN 'column_storage' matched zero columns - storage rows could not be tied to "
            "any model column, so every size below is 0 and none of them are measured. "
            + VERIFIED_JOINS["column_storage"]
        )
    elif real_columns and matched < len(real_columns) * 0.5:
        b.warn(
            f"JOIN 'column_storage' matched only {matched} of {len(real_columns)} columns. "
            + VERIFIED_JOINS["column_storage"]
        )
    if rowsets.get("segments") and not hier_bytes:
        b.warn(
            "JOIN 'hierarchy_bytes' matched zero rows. " + VERIFIED_JOINS["hierarchy_bytes"]
        )
    if rowsets.get("storage_tables") and not any(t.row_count for t in tables.values()):
        b.warn("JOIN 'table_rows' matched zero rows. " + VERIFIED_JOINS["table_rows"])


__all__ = [
    "METADATA_QUERIES",
    "METRIC_QUERIES",
    "QUERIES",
    "UNVERIFIED_JOINS",
    "VERIFIED_JOINS",
    "ContractFinding",
    "DmvQuery",
    "build_model",
    "verify_contract",
]
