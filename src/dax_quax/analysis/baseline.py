"""A scan with memory: save one, compare the next against it, fail a build on the difference.

The same shape as the benchmark `Delta` in timings.py, applied to structure instead of
time. "Sales[Notes] appeared and is 40 MB" and "the model grew 300 MB since main" are the
questions a repo asks that a single scan cannot answer.

TWO THINGS A NAIVE DIFF GETS WRONG
----------------------------------
**Scope.** A baseline taken with two reports and a scan taken with one produce a pile of
verdict changes that say nothing about the model — the scan just looked at less. Comparing
them would report objects "becoming removable" when all that changed is what was scanned.
So verdicts are compared only when both sides saw the same report coverage, and sizes only
when both sides measured anything at all.

**A gate that cannot be evaluated must fail.** If `--fail-on-new-remove` is set and the two
scans are not verdict-comparable, the honest answer is a failed build, not a green one. A CI
check that silently passes because it could not run is worse than no check: it is a check
everyone believes in.

WHAT IT CANNOT DO
-----------------
A rename reads as one object removed and another added. Nothing in the snapshot carries an
identity that survives a rename — TMDL's `lineageTag` would, but the Model does not read it
yet — so the diff says so rather than pretending the pair is unrelated.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dax_quax.analysis.usage import Finding, Verdict, scope_of, summary

if TYPE_CHECKING:
    from dax_quax.model import Model

__all__ = [
    "Baseline",
    "Change",
    "Gates",
    "ObjectSnapshot",
    "StructureDelta",
    "diff",
]

_MB = 1024 * 1024
RENAME_CAVEAT = (
    "a renamed object reads as one removed and one added; nothing in a snapshot survives "
    "a rename"
)


@dataclass(frozen=True, slots=True)
class ObjectSnapshot:
    key: str
    kind: str
    verdict: str
    bytes: int | None = None
    model_refs: int = 0
    report_bindings: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "verdict": self.verdict,
            "bytes": self.bytes,
            "model_refs": self.model_refs,
            "report_bindings": self.report_bindings,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ObjectSnapshot:
        return cls(
            key=str(payload.get("key", "")),
            kind=str(payload.get("kind", "")),
            verdict=str(payload.get("verdict", "")),
            bytes=payload.get("bytes"),
            model_refs=int(payload.get("model_refs") or 0),
            report_bindings=int(payload.get("report_bindings") or 0),
        )


@dataclass(slots=True)
class Baseline:
    """One scan, reduced to what a later comparison needs."""

    model: str = ""
    source: str = ""
    taken_at: str = ""
    scope: str = ""
    reports_scanned: int = 0
    reports_unmatched: int = 0
    reports_unreadable: int = 0
    trustworthy: bool = False
    has_metrics: bool = False
    measured_bytes: int = 0
    objects: dict[str, ObjectSnapshot] = field(default_factory=dict)

    @classmethod
    def from_findings(cls, model: Model, findings: list[Finding]) -> Baseline:
        scope = findings[0].scan_scope if findings else scope_of(model)
        head = summary(findings)
        return cls(
            model=model.name,
            source=model.source,
            taken_at=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            scope=scope.describe(),
            reports_scanned=scope.reports_scanned,
            reports_unmatched=scope.reports_unmatched,
            reports_unreadable=scope.reports_unreadable,
            trustworthy=scope.trustworthy,
            has_metrics=model.has("metrics"),
            measured_bytes=int(head["measured_bytes"]),
            objects={
                finding.key: ObjectSnapshot(
                    key=finding.key,
                    kind=finding.kind,
                    verdict=str(finding.verdict),
                    bytes=finding.bytes,
                    model_refs=finding.model_refs,
                    report_bindings=finding.report_bindings,
                )
                for finding in findings
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "source": self.source,
            "taken_at": self.taken_at,
            "scope": self.scope,
            "reports_scanned": self.reports_scanned,
            "reports_unmatched": self.reports_unmatched,
            "reports_unreadable": self.reports_unreadable,
            "trustworthy": self.trustworthy,
            "has_metrics": self.has_metrics,
            "measured_bytes": self.measured_bytes,
            "objects": [snapshot.to_dict() for snapshot in self.objects.values()],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Baseline:
        objects = [ObjectSnapshot.from_dict(row) for row in payload.get("objects") or []]
        return cls(
            model=str(payload.get("model", "")),
            source=str(payload.get("source", "")),
            taken_at=str(payload.get("taken_at", "")),
            scope=str(payload.get("scope", "")),
            reports_scanned=int(payload.get("reports_scanned") or 0),
            reports_unmatched=int(payload.get("reports_unmatched") or 0),
            reports_unreadable=int(payload.get("reports_unreadable") or 0),
            trustworthy=bool(payload.get("trustworthy")),
            has_metrics=bool(payload.get("has_metrics")),
            measured_bytes=int(payload.get("measured_bytes") or 0),
            objects={snapshot.key: snapshot for snapshot in objects},
        )


@dataclass(frozen=True, slots=True)
class Change:
    key: str
    kind: str
    was: str | None = None
    now: str | None = None
    bytes_before: int | None = None
    bytes_after: int | None = None

    @property
    def bytes_delta(self) -> int | None:
        if self.bytes_before is None or self.bytes_after is None:
            return None
        return self.bytes_after - self.bytes_before

    def describe(self) -> str:
        if self.was is None:
            return f"added   {self.key}  ({self.now}{_size(self.bytes_after)})"
        if self.now is None:
            return f"gone    {self.key}  (was {self.was}{_size(self.bytes_before)})"
        if self.was != self.now:
            return f"verdict {self.key}  {self.was} -> {self.now}"
        return f"size    {self.key}  {_delta(self.bytes_delta)}"


@dataclass(slots=True)
class StructureDelta:
    before: Baseline
    after: Baseline
    added: tuple[Change, ...] = ()
    gone: tuple[Change, ...] = ()
    verdict_changes: tuple[Change, ...] = ()
    size_changes: tuple[Change, ...] = ()
    verdicts_comparable: bool = True
    sizes_comparable: bool = True
    notes: tuple[str, ...] = ()

    # -- the numbers a gate reads --------------------------------------------------------

    @property
    def growth_bytes(self) -> int:
        return self.after.measured_bytes - self.before.measured_bytes

    @property
    def newly_removable(self) -> tuple[Change, ...]:
        """Objects that now report REMOVE and did not before, plus new ones that do."""
        return tuple(
            change
            for change in (*self.verdict_changes, *self.added)
            if change.now == str(Verdict.REMOVE)
        )

    @property
    def newly_unknown(self) -> tuple[Change, ...]:
        return tuple(
            change for change in self.verdict_changes if change.now == str(Verdict.UNKNOWN)
        )

    @property
    def scope_lost(self) -> bool:
        """Whether the scan can defend less than the baseline could."""
        return self.before.trustworthy and not self.after.trustworthy

    @property
    def unchanged(self) -> bool:
        return not (self.added or self.gone or self.verdict_changes or self.size_changes)

    def describe(self) -> str:
        if self.unchanged:
            return "no change"
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.gone:
            parts.append(f"{len(self.gone)} gone")
        if self.verdict_changes:
            parts.append(f"{len(self.verdict_changes)} changed verdict")
        if self.sizes_comparable and self.growth_bytes:
            parts.append(f"{_delta(self.growth_bytes)} overall")
        return ", ".join(parts) or "no change"

    # -- gating ------------------------------------------------------------------------------

    def failures(self, gates: Gates) -> list[str]:
        """Why a build should fail. Empty means it passes.

        A gate that cannot be evaluated fails. Passing a check that did not run is how a
        team ends up trusting a signal that is not there.
        """
        reasons: list[str] = []

        if gates.fail_on_new_remove:
            if not self.verdicts_comparable:
                reasons.append(
                    "--fail-on-new-remove cannot be evaluated: " + self._why_not_verdicts()
                )
            elif self.newly_removable:
                listed = ", ".join(change.key for change in self.newly_removable[:5])
                reasons.append(
                    f"{len(self.newly_removable)} object(s) newly report REMOVE: {listed}"
                )

        if gates.max_growth_bytes is not None:
            if not self.sizes_comparable:
                reasons.append(
                    "--max-growth-mb cannot be evaluated: " + self._why_not_sizes()
                )
            elif self.growth_bytes > gates.max_growth_bytes:
                reasons.append(
                    f"the model grew {_delta(self.growth_bytes)}, over the "
                    f"{_plain(gates.max_growth_bytes)} limit"
                )

        if gates.fail_on_scope_loss and self.scope_lost:
            reasons.append(
                f"scope narrowed: the baseline was {self.before.scope} and this scan is "
                f"{self.after.scope}, so fewer verdicts can be defended than before"
            )
        return reasons

    def _why_not_verdicts(self) -> str:
        return next(
            (note for note in self.notes if "verdict" in note),
            "the two scans did not see the same reports",
        )

    def _why_not_sizes(self) -> str:
        return next(
            (note for note in self.notes if "size" in note), "one side measured nothing"
        )


@dataclass(frozen=True, slots=True)
class Gates:
    """What should fail a build."""

    fail_on_new_remove: bool = False
    max_growth_bytes: int | None = None
    fail_on_scope_loss: bool = False

    @property
    def any(self) -> bool:
        return (
            self.fail_on_new_remove
            or self.max_growth_bytes is not None
            or self.fail_on_scope_loss
        )


def diff(before: Baseline, after: Baseline) -> StructureDelta:
    """Compare two scans, refusing the comparisons that would be meaningless."""
    notes: list[str] = [RENAME_CAVEAT]

    verdicts_comparable = True
    if before.reports_scanned != after.reports_scanned:
        verdicts_comparable = False
        notes.append(
            f"verdict changes are not comparable: the baseline scanned "
            f"{before.reports_scanned} report(s) and this scan {after.reports_scanned}, so a "
            "changed verdict may only mean a different amount was looked at"
        )
    elif before.reports_unmatched != after.reports_unmatched:
        verdicts_comparable = False
        notes.append(
            "verdict changes are not comparable: the two scans differ in how many reports "
            "could be matched to a model, which moves every REMOVE to UNKNOWN on one side"
        )
    elif before.reports_unreadable != after.reports_unreadable:
        verdicts_comparable = False
        notes.append(
            "verdict changes are not comparable: the two scans differ in how many reports "
            "could be read at all, which moves every REMOVE to UNKNOWN on one side"
        )

    sizes_comparable = before.has_metrics and after.has_metrics
    if not sizes_comparable:
        notes.append(
            "size changes are not comparable: "
            + ("the baseline" if not before.has_metrics else "this scan")
            + " has no storage metrics, so every size would read as having gone to nothing"
        )

    if before.model != after.model:
        notes.append(
            f"the baseline is of {before.model!r} and this scan of {after.model!r}; "
            "comparing different models is unlikely to mean anything"
        )

    added, gone, verdict_changes, size_changes = [], [], [], []

    for key, snapshot in after.objects.items():
        previous = before.objects.get(key)
        if previous is None:
            added.append(
                Change(key=key, kind=snapshot.kind, now=snapshot.verdict,
                       bytes_after=snapshot.bytes)
            )
            continue
        if previous.verdict != snapshot.verdict:
            verdict_changes.append(
                Change(
                    key=key,
                    kind=snapshot.kind,
                    was=previous.verdict,
                    now=snapshot.verdict,
                    bytes_before=previous.bytes,
                    bytes_after=snapshot.bytes,
                )
            )
        elif sizes_comparable and previous.bytes != snapshot.bytes:
            size_changes.append(
                Change(
                    key=key,
                    kind=snapshot.kind,
                    was=previous.verdict,
                    now=snapshot.verdict,
                    bytes_before=previous.bytes,
                    bytes_after=snapshot.bytes,
                )
            )

    for key, snapshot in before.objects.items():
        if key not in after.objects:
            gone.append(
                Change(key=key, kind=snapshot.kind, was=snapshot.verdict,
                       bytes_before=snapshot.bytes)
            )

    return StructureDelta(
        before=before,
        after=after,
        added=tuple(sorted(added, key=lambda c: -(c.bytes_after or 0))),
        gone=tuple(sorted(gone, key=lambda c: -(c.bytes_before or 0))),
        verdict_changes=tuple(sorted(verdict_changes, key=lambda c: c.key)),
        size_changes=tuple(sorted(size_changes, key=lambda c: -abs(c.bytes_delta or 0))),
        verdicts_comparable=verdicts_comparable,
        sizes_comparable=sizes_comparable,
        notes=tuple(notes),
    )


def _size(value: int | None) -> str:
    if value is None:
        return ""
    if value >= _MB:
        return f", {value / _MB:.1f} MB"
    if value >= 1024:
        return f", {value / 1024:.1f} KB"
    return f", {value} B"


def _plain(value: int | None) -> str:
    """Standalone size. _size() is for appending and carries its own separator."""
    if value is None:
        return "n/a"
    if value >= _MB:
        return f"{value / _MB:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


def _delta(value: int | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else "-"
    size = abs(value)
    if size >= _MB:
        return f"{sign}{size / _MB:.1f} MB"
    if size >= 1024:
        return f"{sign}{size / 1024:.1f} KB"
    return f"{sign}{size} B"
