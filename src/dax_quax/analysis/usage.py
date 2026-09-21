"""Tiered verdicts: cost joined to usage.

The whole product in one function. For every object: what does it cost, what depends on it,
and can it be deleted?

THE VERDICT IS NEVER A BOOLEAN
------------------------------
``REMOVE`` and ``UNKNOWN`` mean different things and must never be collapsed:

    KEEP     something depends on it
    REVIEW   something depends on it, but it is expensive relative to that use
    REMOVE   nothing references it, and the scan was wide enough to say so
    UNKNOWN  nothing references it, but the scan was not wide enough to say so

Every finding carries ``checked`` (what was examined), ``blind_spots`` (what could not be)
and ``scan_scope`` (how much was covered). A bare "unused" with none of those is an
invitation to delete something load-bearing, and at least one blind spot is permanent:
consumers that are not local .pbip artefacts — Excel, paginated reports, embedded apps,
anything on the XMLA endpoint — are invisible to any local scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from dax_quax.dax.resolve import ModelIndex
from dax_quax.model import Column, Measure, Model

if TYPE_CHECKING:
    import pandas as pd

    from dax_quax.analysis.lineage import LineageGraph

__all__ = [
    "EXTERNAL_REPORTS_CAVEAT",
    "Finding",
    "ScanScope",
    "Thresholds",
    "Verdict",
    "assess",
    "findings_frame",
    "scope_of",
    "summary",
]

#: True of any local scan, so it is stated on every finding rather than in a footnote.
EXTERNAL_REPORTS_CAVEAT = (
    "consumers outside the scanned artefacts — other reports, Excel, paginated reports, "
    "embedded apps, anything on the XMLA endpoint — are invisible to a local scan"
)

_MB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScanScope:
    """What this assessment examined. Carried on every finding.

    A verdict is only as good as its scope, so the scope travels with the finding rather
    than living in a header the reader may never have seen.

    ``reports_unmatched`` is the safety valve for the workspace scan (CONVENTIONS §14). A
    report that could not be tied to its model is *usage that was not counted*, so any
    non-zero value downgrades REMOVE to UNKNOWN.

    ``reports_unreadable`` is the same valve for a report that was found and matched but
    whose bindings could not be read — the legacy single-file ``report.json`` format is the
    common case. It is the more dangerous of the two, because such a report *is* present
    and would otherwise be counted as a report that binds nothing at all.

    ``reports_scanned`` counts only reports that were actually read. A scope where every
    report is unreadable is a scope with no report in it.
    """

    model_source: str = "unknown"
    reports_scanned: int = 0
    report_names: tuple[str, ...] = ()
    reports_unmatched: int = 0
    reports_unreadable: int = 0

    @property
    def any_report(self) -> bool:
        return self.reports_scanned > 0

    @property
    def trustworthy(self) -> bool:
        """Whether a REMOVE verdict is defensible at all under this scope."""
        return self.any_report and self.reports_unmatched == 0 and self.reports_unreadable == 0

    def describe(self) -> str:
        if not self.any_report and not self.reports_unreadable:
            return "no report scanned"
        plural = "s" if self.reports_scanned != 1 else ""
        text = f"{self.reports_scanned} report{plural} scanned"
        if self.reports_unmatched:
            text += f", {self.reports_unmatched} unmatched"
        if self.reports_unreadable:
            text += f", {self.reports_unreadable} unreadable"
        return text


class Verdict(StrEnum):
    KEEP = "KEEP"
    REVIEW = "REVIEW"
    REMOVE = "REMOVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Where a rule starts caring. Tuned per model; these are only a starting point."""

    large_bytes: int = 10 * _MB
    #: distinct values / rows, above which a column is effectively a key
    key_ratio: float = 0.9
    #: distinct values above which a datetime column is worth splitting
    datetime_cardinality: int = 100_000
    #: at or below this many total uses, a large object is worth questioning
    barely_used: int = 1


@dataclass(frozen=True, slots=True)
class Finding:
    key: str
    kind: str
    table: str | None
    name: str
    verdict: Verdict
    bytes: int | None
    pct_of_model: float | None
    model_refs: int
    report_bindings: int
    reason: str
    action: str | None
    checked: tuple[str, ...]
    blind_spots: tuple[str, ...]
    scan_scope: ScanScope = field(default_factory=ScanScope)
    hidden: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.verdict in (Verdict.REMOVE, Verdict.REVIEW)


def scope_of(model: Model) -> ScanScope:
    """Derive a scope from whatever is attached to the model.

    An attached report whose bindings could not be read is counted as unreadable, not as
    scanned. It carries no bindings, so counting it would make every report-bound object
    in the model look unreferenced.
    """
    readable = [r for r in model.reports if r.readable]
    return ScanScope(
        model_source=model.source,
        reports_scanned=len(readable),
        report_names=tuple(r.name for r in readable),
        reports_unreadable=len(model.reports) - len(readable),
    )


def assess(
    model: Model,
    *,
    lineage: LineageGraph | None = None,
    thresholds: Thresholds | None = None,
    scope: ScanScope | None = None,
) -> list[Finding]:
    """Rank every column and measure by cost, joined to how it is used.

    ``scope`` is supplied by a workspace scan that knows how many reports it failed to
    match to a model; otherwise it is derived from the model itself.
    """
    from dax_quax.analysis.lineage import build_lineage

    model.require("expressions")
    lineage = lineage if lineage is not None else build_lineage(model)
    thresholds = thresholds or Thresholds()
    scope = scope if scope is not None else scope_of(model)

    bound = _report_index(model) if scope.any_report else {}

    checked = ["expressions", "dynamic format strings", "relationships", "sortByColumn",
               "groupByColumn"]
    if scope.any_report:
        checked += ["visual field wells", "filters", "conditional formats", "bookmarks"]

    blind_spots = list(lineage.gaps)
    if not scope.any_report and not scope.reports_unreadable:
        blind_spots.insert(0, "the report layer was not scanned")
    if scope.reports_unmatched:
        blind_spots.insert(
            0,
            f"{scope.reports_unmatched} report(s) could not be matched to a model, so their "
            "usage was not counted; every REMOVE has been downgraded to UNKNOWN",
        )
    if scope.reports_unreadable:
        blind_spots.insert(
            0,
            f"{scope.reports_unreadable} report(s) were found but could not be read — most "
            "often the legacy single-file report.json format — so their bindings were not "
            "counted; every REMOVE has been downgraded to UNKNOWN",
        )
    blind_spots.append(EXTERNAL_REPORTS_CAVEAT)

    total = model.total_bytes if model.has("metrics") else 0
    findings: list[Finding] = []

    for obj in [*model.columns.values(), *model.measures.values()]:
        if isinstance(obj, Column) and obj.is_row_number:
            continue
        findings.append(
            _assess_one(
                obj,
                model,
                lineage,
                bound,
                thresholds,
                total,
                scope,
                tuple(checked),
                tuple(blind_spots),
            )
        )

    findings.sort(key=lambda f: (-(f.bytes or 0), f.key))
    return findings


def _assess_one(
    obj: Column | Measure,
    model: Model,
    lineage: LineageGraph,
    bound: dict[str, int],
    thresholds: Thresholds,
    total: int,
    scope: ScanScope,
    checked: tuple[str, ...],
    blind_spots: tuple[str, ...],
) -> Finding:
    key = obj.key
    is_column = isinstance(obj, Column)
    metrics = obj.metrics if is_column else None
    size = metrics.total_bytes if metrics else None

    model_refs = lineage.reference_count(key)
    report_refs = bound.get(key.casefold(), 0)
    used = model_refs > 0 or report_refs > 0

    if not used:
        # REMOVE needs a scope we can defend. An unmatched report is uncounted usage, so
        # it is treated exactly like having scanned no report at all.
        verdict = Verdict.REMOVE if scope.trustworthy else Verdict.UNKNOWN
        reason = _unused_reason(scope)
        action = _removal_action(obj, model) if scope.trustworthy else None
    else:
        rule = _review_rule(obj, model, metrics, model_refs + report_refs, thresholds)
        if rule is None:
            verdict = Verdict.KEEP
            reason = _usage_sentence(model_refs, report_refs)
            action = None
        else:
            verdict = Verdict.REVIEW
            reason, action = rule

    return Finding(
        key=key,
        kind=_kind(obj),
        table=obj.table,
        name=obj.name,
        verdict=verdict,
        bytes=size,
        pct_of_model=(size / total * 100) if size is not None and total else None,
        model_refs=model_refs,
        report_bindings=report_refs,
        reason=reason,
        action=action,
        checked=checked,
        blind_spots=blind_spots,
        scan_scope=scope,
        hidden=obj.is_hidden,
    )


# -- the rules -------------------------------------------------------------------------------
# A small fixed set, deliberately. CONVENTIONS forbids a rules engine here.


def _review_rule(
    obj: Column | Measure,
    model: Model,
    metrics,  # noqa: ANN001 - ColumnMetrics | None
    uses: int,
    thresholds: Thresholds,
) -> tuple[str, str] | None:
    if not isinstance(obj, Column) or metrics is None:
        return None

    table = model.tables.get(obj.table)
    if table is not None and table.is_auto_date_time:
        return (
            "Part of a hidden auto date/time table, which exists only because the feature "
            "is switched on.",
            "Turn off Auto date/time and use a real date table.",
        )

    size = metrics.total_bytes
    rows = metrics.rows or (table.row_count if table else None)
    cardinality = metrics.cardinality

    if (
        size >= thresholds.large_bytes
        and cardinality
        and rows
        and cardinality / rows >= thresholds.key_ratio
    ):
        return (
            f"A near-unique key: {cardinality:,} distinct values over {rows:,} rows, "
            f"costing {_mb(size)}.",
            "Drop it in Power Query unless drill-through needs it, or replace it with an "
            "integer surrogate.",
        )

    if (
        obj.data_type == "DateTime"
        and cardinality
        and cardinality >= thresholds.datetime_cardinality
    ):
        return (
            f"A datetime column with {cardinality:,} distinct values, costing {_mb(size)}.",
            "Split it into a date column and a time column; each compresses far better.",
        )

    if obj.is_calculated and size >= thresholds.large_bytes:
        return (
            f"A calculated column costing {_mb(size)}. Calculated columns are materialised "
            "into storage; measures are not.",
            "Move the logic into a measure, or compute it upstream in Power Query.",
        )

    if size >= thresholds.large_bytes and uses <= thresholds.barely_used:
        return (
            f"{_mb(size)} supporting {uses} use.",
            "Confirm the single use is worth the storage.",
        )

    return None


def _removal_action(obj: Column | Measure, model: Model) -> str | None:
    if isinstance(obj, Measure):
        return "Delete the measure."
    table = model.tables.get(obj.table)
    if table is not None and table.is_auto_date_time:
        return "Turn off Auto date/time; the whole table goes with it."
    if obj.is_calculated:
        return "Delete the calculated column."
    return "Remove it from the Power Query load."


# -- helpers -----------------------------------------------------------------------------------


def _unused_reason(scope: ScanScope) -> str:
    if scope.reports_unreadable:
        return (
            f"Nothing in the model references it, and nothing in the {scope.reports_scanned} "
            f"readable report(s) binds it — but {scope.reports_unreadable} report(s) could not "
            "be read at all, so this is not evidence that it is unused."
        )
    if scope.reports_unmatched:
        return (
            f"Nothing in the model references it, and nothing in the {scope.reports_scanned} "
            f"scanned report(s) binds it — but {scope.reports_unmatched} report(s) could not "
            "be matched to a model, so this is not evidence that it is unused."
        )
    if not scope.any_report:
        return (
            "Nothing in the model references it. The report layer was not scanned, so this "
            "is not yet evidence that it is unused."
        )
    plural = "s" if scope.reports_scanned != 1 else ""
    return (
        "Nothing in the model references it, and no visual, filter, conditional format or "
        f"bookmark in the {scope.reports_scanned} scanned report{plural} binds it."
    )


def _usage_sentence(model_refs: int, report_refs: int) -> str:
    parts = []
    if model_refs:
        parts.append(f"{model_refs} model reference{'s' if model_refs != 1 else ''}")
    if report_refs:
        parts.append(f"{report_refs} report binding{'s' if report_refs != 1 else ''}")
    return "Referenced by " + " and ".join(parts) + "."


def _report_index(model: Model) -> dict[str, int]:
    """Count report bindings per model object, across every attached report.

    Bindings naming something the model does not have are dropped here; the report loader
    already recorded anything it could not parse.
    """
    index = ModelIndex(model)
    counts: dict[str, int] = {}
    for binding in (b for report in model.reports for b in report.bindings):
        if binding.kind == "measure":
            ref = index.measure(binding.property)
        else:
            ref = index.column(binding.entity, binding.property)
            if ref is None:
                # A report may bind a measure with its home table as the entity.
                ref = index.measure(binding.property)
        if ref is not None:
            counts[ref.key.casefold()] = counts.get(ref.key.casefold(), 0) + 1
    return counts


def _kind(obj: Column | Measure) -> str:
    if isinstance(obj, Measure):
        return "measure"
    return "calc_column" if obj.is_calculated else "column"


def _mb(size: int) -> str:
    if size >= _MB:
        return f"{size / _MB:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} bytes"


# -- output ---------------------------------------------------------------------------------------


def findings_frame(findings: list[Finding]) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame.from_records(
        [
            {
                "table": f.table,
                "object": f.name,
                "kind": f.kind,
                "hidden": f.hidden,
                "bytes": f.bytes,
                "pct_of_model": f.pct_of_model,
                "model_refs": f.model_refs,
                "report_bindings": f.report_bindings,
                "verdict": str(f.verdict),
                "reason": f.reason,
                "action": f.action,
                "scan_scope": f.scan_scope.describe(),
            }
            for f in findings
        ],
        columns=[
            "table", "object", "kind", "hidden", "bytes", "pct_of_model",
            "model_refs", "report_bindings", "verdict", "reason", "action", "scan_scope",
        ],
    )


def summary(findings: list[Finding]) -> dict[str, int | float]:
    """The headline numbers. Bytes only count objects the source actually measured."""

    def total(verdict: Verdict) -> int:
        return sum(f.bytes or 0 for f in findings if f.verdict is verdict)

    def count(verdict: Verdict) -> int:
        return sum(1 for f in findings if f.verdict is verdict)

    measured = sum(f.bytes or 0 for f in findings)
    removable = total(Verdict.REMOVE)
    return {
        "objects": len(findings),
        "measured_bytes": measured,
        "removable_bytes": removable,
        "removable_objects": count(Verdict.REMOVE),
        "removable_pct": (removable / measured * 100) if measured else 0.0,
        "review_bytes": total(Verdict.REVIEW),
        "review_objects": count(Verdict.REVIEW),
        "unknown_objects": count(Verdict.UNKNOWN),
        "keep_objects": count(Verdict.KEEP),
    }
