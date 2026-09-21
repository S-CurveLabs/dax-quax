"""The normalised model. A snapshot — no I/O, no live handles, safe to pickle.

Every source produces one of these; every analysis consumes one. Sizes are always an int
count of bytes; formatting happens in render/ and nowhere else.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from dax_quax.errors import MissingCapability

if TYPE_CHECKING:
    import pandas as pd

    from dax_quax.report.pbir import ReportBindings

ObjectKind = Literal["table", "column", "measure", "hierarchy", "calc_item", "visual"]

CAPABILITIES = (
    "metadata", "expressions", "metrics", "report", "dependencies", "execute", "locations",
)

# Power BI's hidden auto date/time tables. Usually pure overhead and always removable
# by turning the feature off, so they are worth recognising by name.
_AUTO_DATE_RE = re.compile(r"^(LocalDateTable|DateTableTemplate)_[0-9a-fA-F-]{8,}")
# The engine's internal row-number column. Real storage, not a user object.
_ROW_NUMBER_RE = re.compile(r"^RowNumber-[0-9A-F-]{8,}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where an object is defined: a file, and a line in it.

    Only a source that read text can produce one. A DMV or .pbix scan leaves it absent
    rather than pointing at a file that is not there, for the same reason `metrics` stays
    None rather than becoming zero: an answer nobody can check is worse than no answer.
    """

    path: pathlib.Path
    line: int | None = None

    def relative_to(self, root: pathlib.Path | None) -> str:
        """The path as a reader would say it: relative to the project when it is inside."""
        if root is not None:
            try:
                return self.path.relative_to(root).as_posix()
            except ValueError:
                pass
        return self.path.as_posix()

    def describe(self, root: pathlib.Path | None = None) -> str:
        where = self.relative_to(root)
        return f"{where}:{self.line}" if self.line else where


@dataclass(frozen=True, slots=True, order=True)
class ObjectRef:
    """Canonical identity. Build these here and never format the key string by hand."""

    kind: ObjectKind
    name: str
    table: str | None = None

    @property
    def key(self) -> str:
        if self.kind == "table":
            return self.name
        if self.kind == "measure":
            return f"[{self.name}]"
        return f"{self.table}[{self.name}]"

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True, slots=True)
class ColumnMetrics:
    """VertiPaq storage facts for one column.

    ``None`` on a field means the source could not measure it. That is categorically
    different from zero and must never be collapsed into one.
    """

    dictionary_bytes: int = 0
    data_bytes: int = 0
    hierarchy_bytes: int = 0
    cardinality: int | None = None
    encoding: str | None = None
    rows: int | None = None

    @property
    def total_bytes(self) -> int:
        return self.dictionary_bytes + self.data_bytes + self.hierarchy_bytes


@dataclass(slots=True)
class Column:
    table: str
    name: str
    data_type: str | None = None
    is_hidden: bool = False
    is_calculated: bool = False
    is_row_number: bool = False
    expression: str | None = None
    sort_by: str | None = None
    #: The column this one groups by — `relatedColumnDetails.groupByColumn` in TMDL. A
    #: field parameter's display column groups by the hidden column holding the field
    #: references, and nothing else in the model or the report mentions that hidden column.
    #: Held as a model key once the loader has resolved it, like `sort_by`.
    group_by: str | None = None
    display_folder: str | None = None
    metrics: ColumnMetrics | None = None

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef("column", self.name, self.table)

    @property
    def key(self) -> str:
        return self.ref.key


@dataclass(slots=True)
class Measure:
    table: str
    name: str
    expression: str | None = None
    is_hidden: bool = False
    display_folder: str | None = None
    format_string: str | None = None
    #: A dynamic format string: DAX, written as `formatStringDefinition` on the measure,
    #: evaluated per cell. Another expression source, and often the only thing referencing
    #: the measure or column that decides the currency symbol.
    format_expression: str | None = None
    description: str | None = None
    #: DAX returned by "show details" on a visual. Another expression source, so another
    #: way a column can be in use without appearing anywhere else.
    detail_rows: str | None = None
    #: A KPI's target, status and trend expressions. DAX, written inside the measure, and
    #: often the only thing referencing the goal measure it compares against.
    kpi_expressions: tuple[str, ...] = ()

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef("measure", self.name, self.table)

    @property
    def key(self) -> str:
        return self.ref.key


@dataclass(slots=True)
class Table:
    name: str
    is_hidden: bool = False
    row_count: int | None = None
    mode: str | None = None
    #: The partition's source text (M, or DAX for a calculated table). Kept because it is
    #: the only place a model records that it reads *another* model.
    source: str | None = None
    #: Whether `source` is DAX rather than M. A calculated table's partition reads
    #: `= calculated` but its mode is still `import`, so mode cannot be used to tell.
    is_calculated: bool = False

    @property
    def is_auto_date_time(self) -> bool:
        return bool(_AUTO_DATE_RE.match(self.name))

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef("table", self.name)

    @property
    def key(self) -> str:
        return self.name


@dataclass(slots=True)
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True
    cross_filter: str | None = None
    from_cardinality: str | None = None
    to_cardinality: str | None = None

    @property
    def endpoints(self) -> tuple[ObjectRef, ObjectRef]:
        return (
            ObjectRef("column", self.from_column, self.from_table),
            ObjectRef("column", self.to_column, self.to_table),
        )


@dataclass(slots=True)
class TablePermission:
    """One table's row filter within a role. The filter is DAX."""

    table: str
    role: str = ""
    filter_expression: str | None = None

    @property
    def key(self) -> str:
        return f"RLS {self.role}/{self.table}" if self.role else f"RLS {self.table}"


@dataclass(slots=True)
class Role:
    name: str
    model_permission: str | None = None
    table_permissions: list[TablePermission] = field(default_factory=list)


@dataclass(slots=True)
class Level:
    name: str
    column: str | None = None
    ordinal: int | None = None


@dataclass(slots=True)
class Hierarchy:
    """A drill path over columns of one table.

    Keyed descriptively rather than as ``Table[Name]``: a hierarchy and a column of the
    same table can share a name, and a collision would merge two graph nodes into one.
    The relationship node does the same thing for the same reason.
    """

    table: str
    name: str
    is_hidden: bool = False
    levels: list[Level] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"hierarchy {self.table}[{self.name}]"


@dataclass(slots=True)
class CalculationItem:
    """One item of a calculation group. Its expression is DAX over SELECTEDMEASURE()."""

    table: str
    name: str
    expression: str | None = None
    format_expression: str | None = None
    ordinal: int | None = None

    @property
    def key(self) -> str:
        return f"calculation item {self.table}[{self.name}]"


@dataclass(slots=True)
class Model:
    name: str
    source: str
    capabilities: frozenset[str] = frozenset()
    tables: dict[str, Table] = field(default_factory=dict)
    columns: dict[str, Column] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)
    hierarchies: list[Hierarchy] = field(default_factory=list)
    calculation_items: list[CalculationItem] = field(default_factory=list)
    scanned_at: _dt.datetime = field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    warnings: list[str] = field(default_factory=list)
    #: Every report scanned against this model. Set by report.attach_report.
    #: A list from the start: a semantic model normally has several thin reports over
    #: it, and assessing against only one is how a live object gets deleted (§14).
    #: Annotation only — model/ never imports report/ at runtime.
    reports: list[ReportBindings] = field(default_factory=list)

    #: Object key -> the file and line that defines it. Only a source that read files can
    #: fill this, so it stays empty for a live or .pbix scan rather than being guessed at.
    #: A missing entry means "this source does not know", never "it is not defined".
    locations: dict[str, SourceRef] = field(default_factory=dict)

    #: The project directory, when the source had one. Paths are shown relative to it.
    root: pathlib.Path | None = None

    def where(self, key: str) -> SourceRef | None:
        return self.locations.get(key)

    # -- capability gate -------------------------------------------------------------

    def require(self, *capabilities: str) -> None:
        needed = set(capabilities)
        if not needed <= set(self.capabilities):
            raise MissingCapability(needed, self.capabilities)

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    # -- lookups ---------------------------------------------------------------------

    def columns_of(self, table: str) -> list[Column]:
        return [c for c in self.columns.values() if c.table == table]

    def measures_of(self, table: str) -> list[Measure]:
        return [m for m in self.measures.values() if m.table == table]

    def resolve(self, key: str) -> Column | Measure | Table | None:
        return self.columns.get(key) or self.measures.get(key) or self.tables.get(key)

    # -- totals ----------------------------------------------------------------------

    @property
    def total_bytes(self) -> int:
        """Sum of every measured column. Unmeasured columns contribute nothing.

        Call ``require("metrics")`` first if a caller needs this to be meaningful.
        """
        return sum(c.metrics.total_bytes for c in self.columns.values() if c.metrics)

    @property
    def measured_column_count(self) -> int:
        return sum(1 for c in self.columns.values() if c.metrics is not None)

    # -- analyses (thin delegates; the real code lives in analysis/) ------------------

    def vertipaq(self) -> pd.DataFrame:
        from dax_quax.analysis.vertipaq import vertipaq

        return vertipaq(self)

    def lineage(self):
        from dax_quax.analysis.lineage import build_lineage

        return build_lineage(self)

    def findings(self, *, thresholds=None, scope=None):
        """Ranked cost x usage. The headline answer, without importing analysis.usage."""
        from dax_quax.analysis.usage import assess

        return assess(self, thresholds=thresholds, scope=scope)

    def findings_frame(self, *, thresholds=None, scope=None) -> pd.DataFrame:
        from dax_quax.analysis.usage import findings_frame

        return findings_frame(self.findings(thresholds=thresholds, scope=scope))

    def __repr__(self) -> str:
        return (
            f"<Model {self.name!r} source={self.source} "
            f"tables={len(self.tables)} columns={len(self.columns)} "
            f"measures={len(self.measures)} caps={','.join(sorted(self.capabilities))}>"
        )


def is_row_number_name(name: str) -> bool:
    return bool(_ROW_NUMBER_RE.match(name))


__all__ = [
    "CAPABILITIES",
    "CalculationItem",
    "Column",
    "ColumnMetrics",
    "Hierarchy",
    "Level",
    "Measure",
    "Model",
    "ObjectRef",
    "Relationship",
    "Role",
    "SourceRef",
    "Table",
    "TablePermission",
    "is_row_number_name",
]
