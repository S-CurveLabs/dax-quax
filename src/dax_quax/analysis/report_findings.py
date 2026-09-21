"""Findings about the report, rather than about the model.

The report layer is already parsed; until now it was only ever asked one question — *is
this object bound* — which is the model's question. Turned around, the same data answers
the report's: what is broken, what is empty, what exists for a single visual.

The most valuable of these is the inverse of everything else in this tool. An object bound
by a visual but **absent from the model** is not a candidate for deletion; it is a visual
that is already broken, today, in production.

WHEN A REPORT IS NOT ABOUT THIS MODEL
-------------------------------------
Every binding then fails to resolve, and a naive pass reports a hundred broken visuals. So
if most bindings miss, the finding is *"this report may not belong to this model"* rather
than a list — the same loud-failure shape as `UNVERIFIED_JOINS` in `sources/dmv.py`.
Unparsed bindings are separate again: those are references the parser could not read, which
is unknown, not broken.
"""

from __future__ import annotations

import pathlib
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from dax_quax.dax.resolve import ModelIndex

if TYPE_CHECKING:
    from dax_quax.model import Model

__all__ = [
    "ALREADY_BROKEN",
    "MISMATCH_RATIO",
    "ReportFinding",
    "report_findings",
    "sort_key",
]

FindingKind = Literal[
    "broken-binding",
    "report-mismatch",
    "empty-visual",
    "empty-page",
    "duplicate-binding",
    "single-use",
]

#: Above this share of unresolvable bindings, the report is assumed to be about a different
#: model. Reporting every binding as broken would bury the real ones.
MISMATCH_RATIO = 0.5

#: The two kinds that are not questions. Everything else here asks whether something should
#: exist; these two say a visual is wrong on screen right now.
ALREADY_BROKEN: tuple[FindingKind, ...] = ("report-mismatch", "broken-binding")

#: Presentation order, shared by the CLI and the HTML report so both read the same way.
_ORDER: dict[str, int] = {
    "report-mismatch": 0,
    "broken-binding": 1,
    "empty-visual": 2,
    "empty-page": 3,
    "duplicate-binding": 4,
    "single-use": 5,
}


def sort_key(finding: ReportFinding) -> tuple[int, str]:
    """Breakage first, then by where it lives."""
    return (_ORDER.get(finding.kind, len(_ORDER)), finding.where)


@dataclass(frozen=True, slots=True)
class ReportFinding:
    kind: FindingKind
    detail: str
    report: str
    page: str | None = None
    visual: str | None = None
    objects: tuple[str, ...] = ()
    #: The report file this is about. R7 links a finding to the thing you would edit.
    file: pathlib.Path | None = None

    @property
    def where(self) -> str:
        parts = [p for p in (self.report, self.page, self.visual) if p]
        return " / ".join(parts)

    @property
    def subject(self) -> str:
        """What you would act on: the object when there is one, else the place.

        An empty page has no object, and that is the point of it.
        """
        return ", ".join(self.objects) if self.objects else self.where

    def describe(self) -> str:
        """One line, for the CLI. The detail carries the location."""
        return f"{self.kind:<18} {self.subject:<28} {self.detail}"


def report_findings(model: Model) -> list[ReportFinding]:
    """Everything worth saying about the reports attached to this model."""
    findings: list[ReportFinding] = []
    index = ModelIndex(model)

    for report in model.reports:
        findings += _broken_bindings(report, index)
        findings += _empty_visuals(report)
        findings += _empty_pages(report)
        findings += _duplicate_bindings(report)

    findings += _single_use(model, index)
    return findings


def _resolve(binding, index: ModelIndex) -> str | None:  # noqa: ANN001
    if binding.kind == "measure":
        reference = index.measure(binding.property)
    else:
        reference = index.column(binding.entity, binding.property) or index.measure(
            binding.property
        )
    return reference.key if reference else None


def _broken_bindings(report, index: ModelIndex) -> list[ReportFinding]:  # noqa: ANN001
    """A visual binding something the model does not have is broken now, not later."""
    broken = [b for b in report.bindings if _resolve(b, index) is None]
    if not broken:
        return []

    if len(broken) / max(1, len(report.bindings)) > MISMATCH_RATIO:
        return [
            ReportFinding(
                kind="report-mismatch",
                report=report.name,
                detail=(
                    f"{len(broken)} of {len(report.bindings)} bindings name objects this "
                    "model does not have. That is more likely to mean the report belongs "
                    "to a different model than that every visual is broken."
                ),
                objects=tuple(sorted({b.key for b in broken})[:8]),
            )
        ]

    return [
        ReportFinding(
            kind="broken-binding",
            report=report.name,
            page=binding.page,
            visual=binding.visual_type or binding.visual,
            # `describe()` leads with the object, so the detail has to say where it lives.
            detail=f"the model does not have it — bound by {binding.where()}",
            objects=(binding.key,),
            file=binding.file,
        )
        for binding in broken
    ]


def _empty_visuals(report) -> list[ReportFinding]:  # noqa: ANN001
    bound = {binding.visual for binding in report.bindings if binding.visual}
    return [
        ReportFinding(
            kind="empty-visual",
            report=report.name,
            page=visual.page,
            visual=visual.visual_type or visual.name,
            detail="a data visual with no fields bound; it renders as an empty frame",
            file=visual.file,
        )
        for visual in report.visuals
        if visual.carries_data and visual.name not in bound
    ]


def _empty_pages(report) -> list[ReportFinding]:  # noqa: ANN001
    with_visuals = {visual.page for visual in report.visuals}
    return [
        ReportFinding(
            kind="empty-page",
            report=report.name,
            page=page,
            detail="a page with no visuals on it",
            file=report.page_files.get(page),
        )
        for page in report.pages
        if page not in with_visuals
    ]


def _duplicate_bindings(report) -> list[ReportFinding]:  # noqa: ANN001
    """The same field bound twice in one visual's field wells.

    Only field wells: a column legitimately appears in both an axis and a filter, and in a
    conditional format alongside either.
    """
    seen: dict[tuple[str | None, str], list[str]] = defaultdict(list)
    files: dict[tuple[str | None, str], pathlib.Path | None] = {}
    for binding in report.bindings:
        if binding.location != "field-well" or not binding.visual:
            continue
        seen[(binding.visual, binding.key)].append(binding.page or "")
        files[(binding.visual, binding.key)] = binding.file

    findings = []
    for (visual, key), pages in sorted(seen.items()):
        if len(pages) > 1:
            findings.append(
                ReportFinding(
                    kind="duplicate-binding",
                    report=report.name,
                    page=pages[0] or None,
                    visual=visual,
                    detail=f"bound {len(pages)} times in the same visual ({visual})",
                    objects=(key,),
                    file=files.get((visual, key)),
                )
            )
    return findings


def _single_use(model: Model, index: ModelIndex) -> list[ReportFinding]:
    """Objects the whole report layer touches exactly once.

    Not a problem on its own. It becomes one next to a size: a column costing 200 MB to
    support one card is a question worth asking, and nothing else in the tool asks it.
    """
    uses: dict[str, list[tuple[str, str, pathlib.Path | None]]] = defaultdict(list)
    for report in model.reports:
        for binding in report.bindings:
            key = _resolve(binding, index)
            if key:
                # `Binding.where()` already opens with the report name.
                uses[key].append((report.name, binding.where(), binding.file))

    findings = []
    for key, places in sorted(uses.items()):
        if len(places) != 1:
            continue
        report_name, place, file = places[0]
        column = model.columns.get(key)
        size = column.metrics.total_bytes if column and column.metrics else None
        detail = f"used once, by {place}"
        if size:
            detail += f" — and costs {_mb(size)}"
        findings.append(
            ReportFinding(
                kind="single-use",
                report=report_name,
                detail=detail,
                objects=(key,),
                file=file,
            )
        )
    return findings


def _mb(size: int) -> str:
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"
