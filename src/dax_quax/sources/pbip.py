"""Load a Model from a .pbip project folder.

The second source, and the one that makes the whole design pay off: the same Model type
comes out, so lineage, usage and rendering work unchanged. What differs is the
capabilities — a PBIP folder has the report layer that a live connection lacks, and lacks
the storage metrics a live connection has, unless its local ``cache.abf`` is present.

    live        metadata expressions metrics dependencies execute
    pbip        metadata expressions                              report
    pbip+cache  metadata expressions metrics                      report

Which is why they are complementary rather than redundant.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from dax_quax.errors import DaxQuaxError
from dax_quax.model import (
    CalculationItem,
    Column,
    Hierarchy,
    Level,
    Measure,
    Model,
    Relationship,
    Role,
    SourceRef,
    Table,
    TablePermission,
)
from dax_quax.sources.tmdl import TmdlNode, parse_tmdl, split_reference, unquote

__all__ = ["PbipProject", "discover", "load_model", "open_pbip"]

#: TMDL data type names mapped to the Model's vocabulary.
_DATA_TYPES = {
    "string": "String",
    "int64": "Int64",
    "double": "Double",
    "decimal": "Decimal",
    "datetime": "DateTime",
    "boolean": "Boolean",
    "binary": "Binary",
    "variant": "Variant",
}

#: TMSL (model.bim) numeric data types, for older PBIP projects.
_TMSL_TYPES = {
    2: "String", 6: "Int64", 8: "Double", 9: "DateTime",
    10: "Decimal", 11: "Boolean", 17: "Binary", 19: "Variant",
}


@dataclass(slots=True)
class PbipProject:
    """What was found on disk. Nothing is loaded yet."""

    root: pathlib.Path
    pbip_file: pathlib.Path | None = None
    semantic_model: pathlib.Path | None = None
    reports: list[pathlib.Path] = field(default_factory=list)
    cache_abf: pathlib.Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        if self.semantic_model is not None:
            return self.semantic_model.name.removesuffix(".SemanticModel").removesuffix(".Dataset")
        return self.root.name


def discover(path: str | pathlib.Path) -> PbipProject:
    """Find the artefacts of a .pbip project.

    Accepts the .pbip file, the project folder, or a .SemanticModel folder directly.
    """
    path = pathlib.Path(path)
    if path.is_file() and path.suffix == ".pbip":
        root, pbip_file = path.parent, path
    elif path.is_dir() and (path.name.endswith(".SemanticModel") or path.name.endswith(".Dataset")):
        root, pbip_file = path.parent, None
    elif path.is_dir():
        root = path
        candidates = sorted(root.glob("*.pbip"))
        pbip_file = candidates[0] if candidates else None
    else:
        raise DaxQuaxError(f"not a .pbip project: {path}")

    project = PbipProject(root=root, pbip_file=pbip_file)

    if path.is_dir() and (path.name.endswith(".SemanticModel") or path.name.endswith(".Dataset")):
        project.semantic_model = path
    else:
        models = sorted(root.glob("*.SemanticModel")) + sorted(root.glob("*.Dataset"))
        if models:
            project.semantic_model = models[0]
        if len(models) > 1:
            project.warnings.append(
                f"{len(models)} semantic models under {root}; using {models[0].name}. "
                "A directory holding several models is a workspace scan, which is phase 2."
            )

    project.reports = sorted(root.glob("*.Report"))

    if project.semantic_model is not None:
        cache = project.semantic_model / ".pbi" / "cache.abf"
        if cache.is_file():
            project.cache_abf = cache

    if project.semantic_model is None:
        project.warnings.append(f"no .SemanticModel folder found under {root}")
    return project


def load_model(path: str | pathlib.Path) -> Model:
    """Load the semantic model only. ``open_pbip`` also attaches the reports."""
    project = discover(path)
    if project.semantic_model is None:
        raise DaxQuaxError(f"no semantic model to load under {project.root}")

    definition = project.semantic_model / "definition"
    model = Model(name=project.name, source="pbip", root=project.root.resolve())
    model.warnings.extend(project.warnings)

    if definition.is_dir():
        _load_tmdl(definition, model)
    elif (project.semantic_model / "model.bim").is_file():
        _load_tmsl(project.semantic_model / "model.bim", model)
    else:
        raise DaxQuaxError(
            f"{project.semantic_model.name} has neither definition/ (TMDL) nor model.bim (TMSL)"
        )

    capabilities = {"metadata", "expressions"}
    # TMSL has no line numbers to offer, so the capability is claimed only when something
    # was actually recorded rather than whenever the source happens to be a directory.
    if model.locations:
        capabilities.add("locations")
    if project.cache_abf is not None:
        if _apply_cache_metrics(project.cache_abf, model):
            capabilities.add("metrics")
    else:
        model.warnings.append(
            "no .pbi/cache.abf in this project, so there are no storage metrics. Refresh the "
            "model in Power BI Desktop, or scan the running instance, to get sizes."
        )
    model.capabilities = frozenset(capabilities)
    return model


def open_pbip(path: str | pathlib.Path, *, attach_reports: bool = True) -> Model:
    """Load a .pbip project: the semantic model plus every sibling report."""
    project = discover(path)
    model = load_model(path)
    if not attach_reports or not project.reports:
        if attach_reports and not project.reports:
            model.warnings.append(
                f"no .Report folder beside {project.name}; unreferenced objects will be "
                "UNKNOWN rather than REMOVE"
            )
        return model

    from dax_quax.report import attach_report, load_report

    attach_report(model, *(load_report(folder) for folder in project.reports))
    return model


# -- TMDL ---------------------------------------------------------------------------------------


def _load_tmdl(definition: pathlib.Path, model: Model) -> None:
    unparsed: list[str] = []
    # The file travels with the node, because a node knows its line but not its file and
    # a finding that says "line 42" without saying of what is worse than saying nothing.
    table_nodes: list[tuple[TmdlNode, pathlib.Path]] = []

    for file in sorted((definition / "tables").glob("*.tmdl")):
        document = parse_tmdl(file.read_text(encoding="utf-8"))
        unparsed += [f"{file.name} {line}" for line in document.unparsed]
        table_nodes += [(n, file) for n in document.nodes if n.keyword == "table"]

    for node, file in table_nodes:
        _add_table(node, model, file)

    for file in sorted((definition / "roles").glob("*.tmdl")) + [definition / "roles.tmdl"]:
        if not file.is_file():
            continue
        document = parse_tmdl(file.read_text(encoding="utf-8"))
        unparsed += [f"{file.name} {line}" for line in document.unparsed]
        for node in document.nodes:
            if node.keyword == "role":
                _add_role(node, model)
                _locate(model, f"role {node.name}", file, node.line)

    relationships_file = definition / "relationships.tmdl"
    if relationships_file.is_file():
        document = parse_tmdl(relationships_file.read_text(encoding="utf-8"))
        unparsed += [f"relationships.tmdl {line}" for line in document.unparsed]
        for node in document.nodes:
            if node.keyword == "relationship":
                _add_relationship(node, model)

    _resolve_sort_by(model)

    if unparsed:
        model.warnings.append(
            f"{len(unparsed)} TMDL line(s) were not understood, so some metadata is missing: "
            + "; ".join(unparsed[:5])
            + (" …" if len(unparsed) > 5 else "")
        )


def _locate(model: Model, key: str, file: pathlib.Path, line: int | None) -> None:
    """Record where an object is written. Absent when a source cannot know."""
    model.locations[key] = SourceRef(path=file.resolve(), line=line or None)


def _add_table(node: TmdlNode, model: Model, file: pathlib.Path | None = None) -> None:
    table = Table(name=node.name, is_hidden=node.flag("isHidden"))
    partition = node.first("partition")
    if partition is not None:
        table.mode = partition.prop("mode") or (
            "calculated" if (partition.value or "").strip() else None
        )
        table.source = partition.prop("source")
        table.is_calculated = (partition.value or "").strip().lower() == "calculated"
    model.tables[table.name] = table
    if file is not None:
        _locate(model, table.name, file, node.line)

    for child in node.children:
        if child.keyword == "column":
            _add_column(child, table.name, model, file)
        elif child.keyword == "measure":
            _add_measure(child, table.name, model, file)
        elif child.keyword == "hierarchy":
            _add_hierarchy(child, table.name, model, file)
        elif child.keyword == "calculationGroup":
            for item in child.find("calculationItem"):
                _add_calculation_item(item, table.name, model, file)


def _add_column(
    node: TmdlNode, table: str, model: Model, file: pathlib.Path | None = None
) -> None:
    data_type = (node.prop("dataType") or "").lower()
    column = Column(
        table=table,
        name=node.name,
        data_type=_DATA_TYPES.get(data_type),
        is_hidden=node.flag("isHidden"),
        # A column with an expression is a calculated column; a plain one has sourceColumn.
        is_calculated=bool(node.value),
        expression=node.value,
        display_folder=node.prop("displayFolder"),
    )
    # Held as the raw name here and turned into a key by _resolve_sort_by, because the
    # target column may live in a file not yet read.
    sort_by = node.prop("sortByColumn")
    if sort_by:
        column.sort_by = unquote(sort_by)
    group_by = _group_by(node)
    if group_by:
        column.group_by = group_by
    model.columns[column.key] = column
    if file is not None:
        _locate(model, column.key, file, node.line)


def _group_by(node: TmdlNode) -> str | None:
    """The `groupByColumn` inside a column's `relatedColumnDetails`.

    A field parameter is a calculated table whose visible column groups by a hidden column
    holding the field references. Nothing else names that hidden column — not DAX, not a
    visual — so without this it reports REMOVE and deleting it breaks the slicer.

    `relatedColumnDetails` opens no block the reader knows about, so its property lands on
    the column itself; both placements are read in case that ever changes.
    """
    details = node.first("relatedColumnDetails")
    raw = (details.prop("groupByColumn") if details else None) or node.prop("groupByColumn")
    return unquote(raw) if raw else None


def _add_measure(
    node: TmdlNode, table: str, model: Model, file: pathlib.Path | None = None
) -> None:
    measure = Measure(
        table=table,
        name=node.name,
        expression=node.value,
        is_hidden=node.flag("isHidden"),
        display_folder=node.prop("displayFolder"),
        format_string=node.prop("formatString"),
        format_expression=_format_expression(node),
        description=node.description,
        detail_rows=_detail_rows(node),
        kpi_expressions=_kpi_expressions(node),
    )
    model.measures[measure.key] = measure
    if file is not None:
        _locate(model, measure.key, file, node.line)


def _kpi_expressions(node: TmdlNode) -> tuple[str, ...]:
    """The DAX inside a measure's `kpi` block.

    A KPI's status expression typically compares the measure against a goal measure that
    nothing else references. Without this, that goal measure reports REMOVE.
    """
    kpi = node.first("kpi")
    if kpi is None:
        return ()
    found = (
        kpi.prop("targetExpression"),
        kpi.prop("statusExpression"),
        kpi.prop("trendExpression"),
    )
    return tuple(expression for expression in found if expression)


def _detail_rows(node: TmdlNode) -> str | None:
    """`detailRowsDefinition` is written either as a property or as its own block."""
    child = node.first("detailRowsDefinition")
    if child is not None and child.value:
        return child.value
    return node.prop("detailRowsDefinition")


def _format_expression(node: TmdlNode) -> str | None:
    """A measure's dynamic format string, written the same two ways as detail rows.

    DAX, and usually the only reference to the measure that decides which symbol to show.
    """
    child = node.first("formatStringDefinition")
    if child is not None and child.value:
        return child.value
    return node.prop("formatStringDefinition")


def _add_hierarchy(
    node: TmdlNode, table: str, model: Model, file: pathlib.Path | None = None
) -> None:
    levels = [
        Level(
            name=level.name,
            column=unquote(level.prop("column") or "") or None,
            ordinal=_int_or_none(level.prop("ordinal")),
        )
        for level in node.find("level")
    ]
    hierarchy = Hierarchy(
        table=table, name=node.name, is_hidden=node.flag("isHidden"), levels=levels
    )
    model.hierarchies.append(hierarchy)
    if file is not None:
        _locate(model, hierarchy.key, file, node.line)


def _add_calculation_item(
    node: TmdlNode, table: str, model: Model, file: pathlib.Path | None = None
) -> None:
    item = CalculationItem(
        table=table,
        name=node.name,
        expression=node.value,
        format_expression=node.prop("formatStringDefinition"),
        ordinal=_int_or_none(node.prop("ordinal")),
    )
    model.calculation_items.append(item)
    if file is not None:
        _locate(model, item.key, file, node.line)


def _add_role(node: TmdlNode, model: Model) -> None:
    permissions = [
        TablePermission(
            table=permission.name,
            role=node.name,
            # `tablePermission Sales = <DAX>`; a role with no filter on a table grants it
            # wholesale, which is not an expression and not a lineage edge.
            filter_expression=permission.value,
        )
        for permission in node.find("tablePermission")
    ]
    model.roles.append(
        Role(
            name=node.name,
            model_permission=node.prop("modelPermission"),
            table_permissions=permissions,
        )
    )


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _add_relationship(node: TmdlNode, model: Model) -> None:
    source = split_reference(node.prop("fromColumn") or "")
    target = split_reference(node.prop("toColumn") or "")
    if not source or not target or not source[0] or not target[0]:
        model.warnings.append(
            f"relationship {node.name}: could not read its endpoints "
            f"({node.prop('fromColumn')!r} -> {node.prop('toColumn')!r}); edge lost"
        )
        return
    is_active = node.properties.get("isActive", "true").strip().lower() not in {"false", "0"}
    model.relationships.append(
        Relationship(
            from_table=source[0],
            from_column=source[1],
            to_table=target[0],
            to_column=target[1],
            is_active=is_active,
            cross_filter=node.prop("crossFilteringBehavior"),
            from_cardinality=node.prop("fromCardinality"),
            to_cardinality=node.prop("toCardinality"),
        )
    )


def _resolve_sort_by(model: Model) -> None:
    """Turn raw sortByColumn / groupByColumn names into model keys.

    Deferred to here because the target column may live in a file not yet read.
    """
    for column in model.columns.values():
        for attribute, label in (("sort_by", "sorts by"), ("group_by", "groups by")):
            target = getattr(column, attribute)
            if not target or "[" in target:
                continue
            key = f"{column.table}[{target}]"
            if key in model.columns:
                setattr(column, attribute, key)
            else:
                setattr(column, attribute, None)
                model.warnings.append(
                    f"{column.key} {label} {target!r}, which is not a column of "
                    f"{column.table}; edge lost"
                )


# -- TMSL (model.bim), for projects saved before TMDL ------------------------------------------


def _load_tmsl(path: pathlib.Path, model: Model) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tables = payload.get("model", {}).get("tables", [])
    for table in tables:
        name = table.get("name")
        if not name:
            continue
        partitions = table.get("partitions") or []
        source = (partitions[0].get("source") or {}) if partitions else {}
        model.tables[name] = Table(
            name=name,
            is_hidden=bool(table.get("isHidden")),
            mode=partitions[0].get("mode") if partitions else None,
            source=_joined(source.get("expression")),
            is_calculated=source.get("type") == "calculated",
        )
        for column in table.get("columns", []):
            column_name = column.get("name")
            if not column_name:
                continue
            obj = Column(
                table=name,
                name=column_name,
                data_type=_TMSL_TYPES.get(column.get("dataType"))
                or str(column.get("dataType") or "").title() or None,
                is_hidden=bool(column.get("isHidden")),
                is_calculated=column.get("type") == "calculated",
                expression=_joined(column.get("expression")),
                display_folder=column.get("displayFolder"),
            )
            if column.get("sortByColumn"):
                obj.sort_by = str(column["sortByColumn"])
            group_by = _tmsl_group_by(column)
            if group_by:
                obj.group_by = group_by
            model.columns[obj.key] = obj
        for hierarchy in table.get("hierarchies", []):
            hierarchy_name = hierarchy.get("name")
            if not hierarchy_name:
                continue
            model.hierarchies.append(
                Hierarchy(
                    table=name,
                    name=hierarchy_name,
                    is_hidden=bool(hierarchy.get("isHidden")),
                    levels=[
                        Level(
                            name=level.get("name") or "",
                            column=str(level["column"]) if level.get("column") else None,
                            ordinal=level.get("ordinal"),
                        )
                        for level in hierarchy.get("levels", [])
                    ],
                )
            )
        for group in (table.get("calculationGroup") or {}).get("calculationItems") or []:
            item_name = group.get("name")
            if not item_name:
                continue
            model.calculation_items.append(
                CalculationItem(
                    table=name,
                    name=item_name,
                    expression=_joined(group.get("expression")),
                    format_expression=_joined(
                        (group.get("formatStringDefinition") or {}).get("expression")
                    ),
                    ordinal=group.get("ordinal"),
                )
            )
        for measure in table.get("measures", []):
            measure_name = measure.get("name")
            if not measure_name:
                continue
            obj = Measure(
                table=name,
                name=measure_name,
                expression=_joined(measure.get("expression")),
                is_hidden=bool(measure.get("isHidden")),
                display_folder=measure.get("displayFolder"),
                format_string=measure.get("formatString"),
                format_expression=_joined(
                    (measure.get("formatStringDefinition") or {}).get("expression")
                    if isinstance(measure.get("formatStringDefinition"), dict)
                    else measure.get("formatStringDefinition")
                ),
                description=_joined(measure.get("description")),
                detail_rows=_joined(
                    (measure.get("detailRowsDefinition") or {}).get("expression")
                    if isinstance(measure.get("detailRowsDefinition"), dict)
                    else measure.get("detailRowsDefinition")
                ),
                kpi_expressions=_tmsl_kpi(measure.get("kpi")),
            )
            model.measures[obj.key] = obj

    for role in payload.get("model", {}).get("roles", []) or []:
        role_name = role.get("name")
        if not role_name:
            continue
        model.roles.append(
            Role(
                name=role_name,
                model_permission=role.get("modelPermission"),
                table_permissions=[
                    TablePermission(
                        table=str(permission.get("table") or ""),
                        role=role_name,
                        filter_expression=_joined(permission.get("filterExpression")),
                    )
                    for permission in role.get("tablePermissions", [])
                    if permission.get("table")
                ],
            )
        )

    for relationship in payload.get("model", {}).get("relationships", []):
        model.relationships.append(
            Relationship(
                from_table=relationship.get("fromTable", ""),
                from_column=relationship.get("fromColumn", ""),
                to_table=relationship.get("toTable", ""),
                to_column=relationship.get("toColumn", ""),
                is_active=relationship.get("isActive", True),
                cross_filter=relationship.get("crossFilteringBehavior"),
            )
        )
    _resolve_sort_by(model)


def _tmsl_group_by(column: dict) -> str | None:
    """`relatedColumnDetails.groupByColumns[].groupingColumn` — the TMSL spelling.

    Same object as TMDL's `groupByColumn` and the same danger if it is missed: a field
    parameter's hidden column is referenced from nowhere else. TMSL allows several, and
    only the first is kept because Column carries one target; a second would be a lost
    edge, so it is warned about by the caller rather than dropped here.
    """
    details = column.get("relatedColumnDetails")
    if not isinstance(details, dict):
        return None
    for entry in details.get("groupByColumns") or []:
        if isinstance(entry, dict) and entry.get("groupingColumn"):
            return str(entry["groupingColumn"])
    return None


def _tmsl_kpi(kpi: object) -> tuple[str, ...]:
    """A KPI's target, status and trend expressions, as TMSL spells them."""
    if not isinstance(kpi, dict):
        return ()
    found = (
        _joined(kpi.get("targetExpression")),
        _joined(kpi.get("statusExpression")),
        _joined(kpi.get("trendExpression")),
    )
    return tuple(expression for expression in found if expression)


def _joined(value: object) -> str | None:
    """TMSL stores multi-line expressions as a list of lines."""
    if value is None:
        return None
    if isinstance(value, list):
        return "\n".join(str(line) for line in value)
    return str(value)


# -- storage metrics from the local data cache ----------------------------------------------------


def _apply_cache_metrics(cache: pathlib.Path, model: Model) -> bool:
    """Read VertiPaq metrics out of .pbi/cache.abf.

    UNVERIFIED: the extraction has never run against a real cache.abf. The failure paths —
    pbixray absent, cache unreadable — are the tested ones, and both leave the model
    without the 'metrics' capability rather than with zeros.
    """
    try:
        import pbixray  # noqa: F401
    except ImportError:
        model.warnings.append(
            f"{cache.name} is present but pbixray is not installed, so there are no storage "
            "metrics. Install the 'file' extra: pip install dax-quax[file]"
        )
        return False

    try:
        return _read_abf_metrics(cache, model)
    except Exception as exc:  # noqa: BLE001 - a third-party parser over a binary format
        model.warnings.append(f"could not read {cache.name}: {type(exc).__name__}: {exc}")
        return False


def _read_abf_metrics(cache: pathlib.Path, model: Model) -> bool:
    """Read a project's local data cache through the same contract the .pbix source uses.

    This used to carry its own guess at pbixray's column names. Two copies of one contract
    is how the escaping rule drifted; there is one now, in sources/file.py.
    """
    from pbixray import PBIXRay

    from dax_quax.sources.file import apply_statistics

    source = PBIXRay(str(cache))
    try:
        matched = apply_statistics(source, model)
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()

    if matched == 0:
        model.warnings.append(
            f"{cache.name} was read but none of its columns matched the model; "
            "storage metrics are unavailable"
        )
        return False
    return True
