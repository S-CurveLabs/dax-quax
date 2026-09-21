"""Server timings: where a query's time actually went.

A DAX query splits across two engines. The storage engine (VertiPaq) scans compressed
columns, is massively parallel, and caches. The formula engine walks the result row by row,
is single-threaded, and caches nothing. An optimisation is only real if it moves time out of
the formula engine or removes storage-engine scans, and this module is how you tell.

Pure, like ``sources/dmv.py``. It takes trace event dictionaries and computes; it never
opens a trace. ``sources/trace.py`` produces the events. That split is what makes the
arithmetic testable on a machine with no Power BI Desktop.

ON THE ACCURACY OF THIS FILE
----------------------------
The event shapes and the subclass filter below are declared assumptions that have never
been checked against a real trace. They are registered in :data:`UNVERIFIED_TIMINGS`, and
:func:`parse_events` reports anything it could not account for rather than folding it into
a number that looks authoritative.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "UNVERIFIED_TIMINGS",
    "Benchmark",
    "Delta",
    "PlanLine",
    "QueryPlan",
    "SeQuery",
    "Timings",
    "compare",
    "parse_events",
    "parse_query_plan",
    "summarise",
]

Event = dict[str, Any]

#: Trace event classes this module reads, by name and by the numeric id AMO also uses.
#: Names as a real trace emits them, observed against Power BI Desktop 2.157.1354.0 on
#: 2026-09-19. They are the AMO enum's own str(), which is PascalCase with no spaces --
#: the previous guesses wrote "VertiPaq Scan" and "DAX VertiPaq Logical Plan", and neither
#: matches anything. The numeric codes stay as aliases: they are what the raw wire value
#: looks like if anything ever reads a column through the indexer rather than the property.
EVENT_CLASSES: dict[str, tuple[str, ...]] = {
    "query_begin": ("QueryBegin", "9"),
    "query_end": ("QueryEnd", "10"),
    "se_query_begin": ("VertiPaqSEQueryBegin", "82"),
    "se_query_end": ("VertiPaqSEQueryEnd", "83"),
    "se_cache_match": ("VertiPaqSEQueryCacheMatch", "84"),
    "query_plan": ("DAXQueryPlan", "85", "88"),
    "direct_query_end": ("DirectQueryEnd", "97"),
}

#: The begin events carry no duration, so nothing is computed from them. They are named
#: here so they are *recognised and ignored* rather than reported as unaccounted noise.
IGNORED_CLASSES = frozenset({"query_begin", "se_query_begin"})

#: QueryEnd subclasses. Only a DAX query is the query being timed: a DmxQuery QueryEnd is
#: a DMV read on the same session, and letting one set `total_ms` would time the wrong
#: statement entirely.
QUERY_SUBCLASSES = frozenset({"0", "DAXQuery"})

#: VertiPaqSEQueryEnd subclasses. Only a scan is a real storage-engine query; the internal
#: variant restates work already counted, so summing every subclass double-counts SE time.
SE_SCAN_SUBCLASSES = frozenset({"0", "VertiPaqScan"})
SE_INTERNAL_SUBCLASSES = frozenset({"1", "10", "VertiPaqScanInternal", "BatchVertiPaqScan"})

#: DAXQueryPlan subclasses.
PLAN_LOGICAL = frozenset({"1", "DAXVertiPaqLogicalPlan"})
PLAN_PHYSICAL = frozenset({"2", "DAXVertiPaqPhysicalPlan"})

UNVERIFIED_TIMINGS: dict[str, str] = {
    "se_subclass_filter": (
        "storage-engine time is summed only over VertiPaqSEQueryEnd events whose "
        "EventSubclass is a scan (0). Internal and batch subclasses are assumed to restate "
        "work already counted."
    ),
    "fe_by_subtraction": (
        "formula-engine time is QueryEnd duration minus storage-engine duration. It is a "
        "residual, not a measurement, so anything the storage engine does that this filter "
        "misses lands in the formula engine's column."
    ),
    "duration_units": (
        "Duration and CpuTime are taken to be milliseconds, as AMO reports them."
    ),
}

_RECORDS = re.compile(r"#Records\s*=\s*(\d+)")


# -- one query's timings -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeQuery:
    """One storage-engine scan."""

    duration_ms: int
    cpu_ms: int
    rows: int | None = None
    text: str | None = None
    subclass: str | None = None

    @property
    def is_cached(self) -> bool:
        return self.duration_ms == 0 and self.cpu_ms == 0


@dataclass(frozen=True, slots=True)
class Timings:
    total_ms: int = 0
    se_ms: int = 0
    se_cpu_ms: int = 0
    se_queries: tuple[SeQuery, ...] = ()
    cache_matches: int = 0
    rows: int | None = None
    plans: tuple[QueryPlan, ...] = ()
    unaccounted: tuple[str, ...] = ()

    @property
    def fe_ms(self) -> int:
        """Formula-engine time. A residual — see UNVERIFIED_TIMINGS."""
        return max(0, self.total_ms - self.se_ms)

    @property
    def fe_pct(self) -> float:
        return (self.fe_ms / self.total_ms * 100) if self.total_ms else 0.0

    @property
    def se_pct(self) -> float:
        return (self.se_ms / self.total_ms * 100) if self.total_ms else 0.0

    @property
    def se_query_count(self) -> int:
        return len(self.se_queries)

    @property
    def parallelism(self) -> float:
        """Storage-engine CPU over elapsed. Below ~1 means the scans did not parallelise."""
        return (self.se_cpu_ms / self.se_ms) if self.se_ms else 0.0

    def describe(self) -> str:
        return (
            f"{self.total_ms} ms total — FE {self.fe_ms} ms ({self.fe_pct:.0f}%), "
            f"SE {self.se_ms} ms ({self.se_pct:.0f}%) over {self.se_query_count} scan(s), "
            f"{self.cache_matches} cache hit(s)"
        )


def _class_of(event: Event) -> str | None:
    raw = str(event.get("EventClass", "")).strip()
    for name, aliases in EVENT_CLASSES.items():
        if raw in aliases:
            return name
    return None


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_events(events: list[Event]) -> Timings:
    """Fold trace events into one query's timings."""
    total = 0
    rows: int | None = None
    se_ms = se_cpu = 0
    scans: list[SeQuery] = []
    cache_matches = 0
    plans: list[QueryPlan] = []
    unaccounted: list[str] = []

    for event in events:
        kind = _class_of(event)
        subclass = str(event.get("EventSubclass", "")).strip()

        if kind in IGNORED_CLASSES:
            continue
        if kind == "query_end":
            if subclass and subclass not in QUERY_SUBCLASSES:
                continue  # a DMV read on the same session, not the query being timed
            total = _int(event.get("Duration"))
            if event.get("RowNumber") is not None:
                rows = _int(event.get("RowNumber"))
        elif kind == "se_query_end":
            scan = SeQuery(
                duration_ms=_int(event.get("Duration")),
                cpu_ms=_int(event.get("CpuTime")),
                rows=_records_in(event.get("TextData")),
                text=_text(event.get("TextData")),
                subclass=subclass or None,
            )
            if subclass in SE_SCAN_SUBCLASSES:
                scans.append(scan)
                se_ms += scan.duration_ms
                se_cpu += scan.cpu_ms
            elif subclass not in SE_INTERNAL_SUBCLASSES:
                unaccounted.append(
                    f"VertiPaqSEQueryEnd with unrecognised EventSubclass {subclass!r}; "
                    "its time is not counted as storage engine"
                )
        elif kind == "se_cache_match":
            cache_matches += 1
        elif kind == "query_plan":
            plan = parse_query_plan(
                _text(event.get("TextData")) or "",
                "physical" if subclass in PLAN_PHYSICAL else "logical",
            )
            if plan.lines:
                plans.append(plan)
        elif kind == "direct_query_end":
            se_ms += _int(event.get("Duration"))
        elif kind is None and event.get("EventClass"):
            unaccounted.append(f"unrecognised EventClass {event['EventClass']!r}")

    return Timings(
        total_ms=total,
        se_ms=se_ms,
        se_cpu_ms=se_cpu,
        se_queries=tuple(scans),
        cache_matches=cache_matches,
        rows=rows,
        plans=tuple(plans),
        unaccounted=tuple(dict.fromkeys(unaccounted)),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _records_in(text: Any) -> int | None:
    match = _RECORDS.search(str(text or ""))
    return int(match.group(1)) if match else None


# -- query plans -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanLine:
    depth: int
    text: str
    records: int | None = None


@dataclass(frozen=True, slots=True)
class QueryPlan:
    kind: Literal["logical", "physical"]
    lines: tuple[PlanLine, ...] = ()

    @property
    def widest(self) -> PlanLine | None:
        """The line touching the most records — usually where the time went."""
        counted = [line for line in self.lines if line.records is not None]
        return max(counted, key=lambda line: line.records or 0) if counted else None


def parse_query_plan(text: str, kind: Literal["logical", "physical"]) -> QueryPlan:
    """Turn a plan's text into an indentation tree.

    Plans nest by leading whitespace, and a physical plan annotates rows as ``#Records=N``.
    """
    lines: list[PlanLine] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stripped = raw.lstrip("\t ")
        depth = len(raw) - len(stripped)
        # A tab is one level; spaces are counted in fours, matching how the engine emits it.
        depth = raw[: len(raw) - len(stripped)].count("\t") + (
            raw[: len(raw) - len(stripped)].count(" ") // 4
        )
        lines.append(PlanLine(depth=depth, text=stripped, records=_records_in(stripped)))
    return QueryPlan(kind=kind, lines=tuple(lines))


# -- benchmarking ------------------------------------------------------------------------------


@dataclass(slots=True)
class Benchmark:
    """Several runs of one query."""

    query: str
    runs: list[Timings] = field(default_factory=list)
    cold: bool = False
    label: str | None = None
    errors: list[str] = field(default_factory=list)

    def _values(self, attribute: str) -> list[int]:
        return [getattr(run, attribute) for run in self.runs]

    @property
    def ok(self) -> bool:
        return bool(self.runs) and not self.errors

    @property
    def median_total_ms(self) -> int:
        """Median, not mean: one stalled run should not move the headline."""
        return int(statistics.median(self._values("total_ms"))) if self.runs else 0

    @property
    def median_se_ms(self) -> int:
        return int(statistics.median(self._values("se_ms"))) if self.runs else 0

    @property
    def median_fe_ms(self) -> int:
        return int(statistics.median(self._values("fe_ms"))) if self.runs else 0

    @property
    def best_total_ms(self) -> int:
        return min(self._values("total_ms")) if self.runs else 0

    @property
    def spread_ms(self) -> int:
        values = self._values("total_ms")
        return (max(values) - min(values)) if values else 0

    @property
    def se_query_count(self) -> int:
        return int(statistics.median([r.se_query_count for r in self.runs])) if self.runs else 0

    # -- persistence -------------------------------------------------------------------
    # A before/after comparison usually spans a model edit, so "before" has to survive on
    # disk. Only the numbers a Delta reads are kept; scan text and plans are not.

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "label": self.label,
            "cold": self.cold,
            "errors": list(self.errors),
            "runs": [
                {
                    "total_ms": run.total_ms,
                    "se_ms": run.se_ms,
                    "se_cpu_ms": run.se_cpu_ms,
                    "cache_matches": run.cache_matches,
                    "se_queries": run.se_query_count,
                }
                for run in self.runs
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Benchmark:
        result = cls(
            query=str(payload.get("query", "")),
            label=payload.get("label"),
            cold=bool(payload.get("cold")),
            errors=list(payload.get("errors") or []),
        )
        for run in payload.get("runs") or []:
            # Placeholder scans preserve the count, which is all a Delta reads.
            count = int(run.get("se_queries") or 0)
            result.runs.append(
                Timings(
                    total_ms=int(run.get("total_ms") or 0),
                    se_ms=int(run.get("se_ms") or 0),
                    se_cpu_ms=int(run.get("se_cpu_ms") or 0),
                    se_queries=tuple(SeQuery(0, 0) for _ in range(count)),
                    cache_matches=int(run.get("cache_matches") or 0),
                )
            )
        return result


# -- before and after ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Delta:
    """The M7 acceptance shape: what changed between two benchmarks."""

    before: Benchmark
    after: Benchmark

    def _shift(self, attribute: str) -> int:
        return getattr(self.after, attribute) - getattr(self.before, attribute)

    @property
    def total_ms(self) -> int:
        return self._shift("median_total_ms")

    @property
    def se_ms(self) -> int:
        return self._shift("median_se_ms")

    @property
    def fe_ms(self) -> int:
        return self._shift("median_fe_ms")

    @property
    def se_queries(self) -> int:
        return self._shift("se_query_count")

    @property
    def total_pct(self) -> float:
        base = self.before.median_total_ms
        return (self.total_ms / base * 100) if base else 0.0

    @property
    def improved(self) -> bool:
        return self.total_ms < 0

    @property
    def within_noise(self) -> bool:
        """Whether the change is smaller than the runs' own variation.

        Reporting a 3 ms win on runs that vary by 40 ms is how people convince themselves
        an optimisation worked.
        """
        noise = max(self.before.spread_ms, self.after.spread_ms)
        return abs(self.total_ms) <= noise

    def describe(self) -> str:
        if not (self.before.ok and self.after.ok):
            return "one of the two benchmarks did not run"
        direction = "faster" if self.improved else "slower"
        text = (
            f"{abs(self.total_ms)} ms {direction} "
            f"({self.before.median_total_ms} -> {self.after.median_total_ms} ms, "
            f"{self.total_pct:+.0f}%)"
        )
        if self.within_noise:
            text += (
                f" — but the runs themselves vary by up to "
                f"{max(self.before.spread_ms, self.after.spread_ms)} ms, so this is noise"
            )
        return text


def compare(before: Benchmark, after: Benchmark) -> Delta:
    return Delta(before=before, after=after)


def summarise(benchmark: Benchmark) -> dict[str, Any]:
    return {
        "label": benchmark.label,
        "runs": len(benchmark.runs),
        "cold": benchmark.cold,
        "median_total_ms": benchmark.median_total_ms,
        "median_se_ms": benchmark.median_se_ms,
        "median_fe_ms": benchmark.median_fe_ms,
        "best_total_ms": benchmark.best_total_ms,
        "spread_ms": benchmark.spread_ms,
        "se_queries": benchmark.se_query_count,
        "errors": list(benchmark.errors),
    }
