"""The tool surface an agent sees, as plain functions over a Model.

This module has no MCP dependency at all. It is the whole server minus the transport, so
every tool is testable without a client, and `server.py` is thin enough to read in one
sitting. Same arrangement as the CLI: the library is the product, this is a consumer.

AN AGENT WILL ACT ON A VERDICT
------------------------------
Everywhere else in this tool a verdict is read by a person who saw the scope headline on
the way in. An agent has no way in — it calls one tool and gets one payload, and if that
payload says ``REMOVE`` with nothing beside it, the next thing it writes is a deletion.

So the caveat is not left to each handler to remember. `call` builds the envelope, looks
through the payload for a verdict, and attaches the scope and the caveat when it finds
one. A tool cannot return a verdict without them, because it does not get a say.

READ-ONLY, TWICE
----------------
CONVENTIONS §15 makes R5 conditional on this surface staying read-only, so it is fenced
two ways rather than one:

1. Every tool is a function of a `Model`, and §9 defines a Model as a snapshot that holds
   no connection. It cannot run a query, let alone a write. This is the real guarantee;
   the fence below is only there to keep it true.
2. `EXPOSED` names what is reachable. A handler added to `TOOLS` is not callable until it
   is also named there — two deliberate edits, in a module whose docstring says why.

Phase 3 (§16) would add tools that produce changes. It stays fenced: R5 plus an unfenced
phase 3 is an agent that can delete columns, which is not a thing either section agreed
to on its own.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dax_quax.analysis.usage import Finding, Thresholds, assess, scope_of, summary
from dax_quax.errors import DaxQuaxError
from dax_quax.model import Model

__all__ = [
    "EXPOSED",
    "TOOLS",
    "Session",
    "ToolSpec",
    "call",
    "tool_list",
]

#: How much DAX to hand back before it stops being context and starts being a transcript.
MAX_EXPRESSION_CHARS = 2000

#: What a REMOVE actually claims. Attached to any payload that carries a verdict.
VERDICT_CAVEAT = (
    "A REMOVE verdict means nothing in what was scanned referenced the object. It is a "
    "question to put to a person, not an instruction to delete. Check `blind_spots` on "
    "the object before proposing any change."
)

_UNTRUSTWORTHY_CAVEAT = (
    "No report was scanned over this model, or a report could not be matched to it, so "
    "every unreferenced object is UNKNOWN rather than REMOVE. Usage that was not counted "
    "is not the same as an object that is unused."
)


@dataclass(slots=True)
class Session:
    """One model, held across calls, with the analyses it was asked for.

    An agent asks twenty questions about the same model, so re-reading the source per call
    would be absurd. It is held, and `scanned_at` rides on every response so a stale answer
    is visible as one rather than passing for a fresh one.

    Deliberately not `render.serve.State`: that one carries Jinja templates and lives
    behind the [serve] extra. The shape is the same on purpose — a loader, not a Model,
    because the thing being examined is usually open in Power BI Desktop and changing.
    """

    loader: Callable[[], Model]
    description: str = "source"
    thresholds: Thresholds | None = None
    model: Model | None = None
    lineage: Any = None
    findings: tuple[Finding, ...] = ()
    loaded_at: _dt.datetime | None = None
    scans: int = field(default=0)

    def rescan(self) -> None:
        """Re-read the source.

        A failure propagates. `serve` swallows one and shows it in the page, because a
        person can see the page is broken; an agent handed an empty model would conclude
        the model is empty, which is the false-REMOVE shape this whole tool refuses.
        """
        from dax_quax.analysis.lineage import build_lineage

        model = self.loader()
        lineage = build_lineage(model)
        self.model = model
        self.lineage = lineage
        self.findings = tuple(assess(model, lineage=lineage, thresholds=self.thresholds))
        self.loaded_at = _dt.datetime.now(_dt.UTC)
        self.scans += 1

    def ensure(self) -> Model:
        if self.model is None:
            self.rescan()
        if self.model is None:  # pragma: no cover - rescan raises rather than returning
            raise DaxQuaxError("no model loaded")
        return self.model


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    summary: str
    schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]]


# -- formatting ------------------------------------------------------------------------


def _bytes(value: int | None) -> str | None:
    """None stays None. '0 B' for something unmeasured is the lie this tool exists to avoid."""
    from dax_quax.render.report import format_bytes

    return None if value is None else format_bytes(value)


def _clip(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    if len(text) <= MAX_EXPRESSION_CHARS:
        return {"text": text}
    return {
        "text": text[:MAX_EXPRESSION_CHARS],
        "truncated_chars": len(text) - MAX_EXPRESSION_CHARS,
    }


def _row(finding: Finding) -> dict[str, Any]:
    return {
        "key": finding.key,
        "kind": finding.kind,
        "verdict": str(finding.verdict),
        "bytes": finding.bytes,
        "size": _bytes(finding.bytes),
        "pct_of_model": finding.pct_of_model,
        "model_refs": finding.model_refs,
        "report_bindings": finding.report_bindings,
        "reason": finding.reason,
    }


def _relative(path: Any, root: Any) -> str | None:
    """A path the reader can act on: relative to the project when it is inside one."""
    if path is None:
        return None
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _page(rows: list[Any], limit: int) -> dict[str, Any]:
    """Every list this server returns says what it left out."""
    shown = rows[:limit]
    return {"items": shown, "shown": len(shown), "matched": len(rows)}


def _resolve(model: Model, key: str) -> tuple[str | None, list[str]]:
    """Find the object an agent meant.

    It will say "Total Sales", "[Total Sales]" or "Sales[Amount]" interchangeably, so all
    three resolve. A miss returns suggestions rather than nothing: a bare "not found" sends
    an agent guessing, and a guessing agent invents object names.
    """
    from dax_quax.dax.resolve import ModelIndex

    if key in model.columns or key in model.measures or key in model.tables:
        return key, []

    index = ModelIndex(model)
    bare = key.strip()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1]
    if "[" in bare and bare.endswith("]"):
        table, _, column = bare[:-1].partition("[")
        reference = index.column(table.strip("'"), column)
        if reference:
            return reference.key, []
        # A measure written as `Table[Measure]`. Discouraged in DAX and written constantly.
        bare = column
    reference = index.measure(bare)
    if reference:
        return reference.key, []

    needle = bare.casefold()
    pool = [*model.measures, *model.columns, *model.tables]
    near = [candidate for candidate in pool if needle in candidate.casefold()]
    return None, sorted(near)[:10]


# -- handlers --------------------------------------------------------------------------


def _describe_model(session: Session) -> dict[str, Any]:
    model = session.ensure()
    head = summary(list(session.findings))
    return {
        "capabilities": sorted(model.capabilities),
        "source_files": (
            f"objects carry the file and line that define them, relative to {model.root}"
            if model.has("locations")
            else "this source has no files, so no object can say where it is defined"
        ),
        "counts": {
            "tables": len(model.tables),
            "columns": sum(1 for c in model.columns.values() if not c.is_row_number),
            "measures": len(model.measures),
            "relationships": len(model.relationships),
        },
        "reports": [
            {"name": r.name, "pages": len(r.pages), "visuals": r.visual_count}
            for r in model.reports
        ],
        # `summary` counts bytes as ints, and a source with no metrics counts zero of them.
        # Handing an agent "0 B measured" would read as "this model is free".
        "summary": (
            {
                **head,
                "measured_size": _bytes(head["measured_bytes"]),
                "removable_size": _bytes(head["removable_bytes"]),
            }
            if model.has("metrics")
            else {
                **{k: v for k, v in head.items() if not k.endswith("bytes")},
                "sizes": "this source carries no storage metrics; no size here is measured",
            }
        ),
        # Not decoration. Every one of these is a reason a verdict below may be wrong.
        "warnings": list(model.warnings),
        "blind_spots": list(session.findings[0].blind_spots) if session.findings else [],
    }


def _search_objects(
    session: Session, query: str, kind: str | None = None, limit: int = 25
) -> dict[str, Any]:
    session.ensure()
    needle = query.casefold()
    rows = [
        _row(finding)
        for finding in session.findings
        if needle in finding.key.casefold() and (kind is None or finding.kind == kind)
    ]
    rows.sort(key=lambda r: (-(r["bytes"] or 0), r["key"]))
    return {"query": query, **_page(rows, limit)}


def _list_findings(
    session: Session,
    verdict: str | None = None,
    kind: str | None = None,
    min_mb: float = 0.0,
    limit: int = 25,
) -> dict[str, Any]:
    session.ensure()
    floor = int(min_mb * 1024 * 1024)
    rows = [
        _row(finding)
        for finding in session.findings
        if (verdict is None or str(finding.verdict) == verdict)
        and (kind is None or finding.kind == kind)
        and (finding.bytes or 0) >= floor
    ]
    return {"filter": {"verdict": verdict, "kind": kind, "min_mb": min_mb}, **_page(rows, limit)}


def _explain_object(session: Session, key: str) -> dict[str, Any]:
    """Everything known about one object, which is the call an agent makes before advising."""
    from dax_quax.report import bindings_for

    model = session.ensure()
    resolved, suggestions = _resolve(model, key)
    if resolved is None:
        return {"asked_for": key, "found": False, "did_you_mean": suggestions}

    finding = next((f for f in session.findings if f.key == resolved), None)
    column = model.columns.get(resolved)
    measure = model.measures.get(resolved)
    metrics = column.metrics if column else None
    lineage = session.lineage

    defined_by = measure or column
    payload: dict[str, Any] = {
        "asked_for": key,
        "found": True,
        "key": resolved,
        "expression": _clip(defined_by.expression if defined_by else None),
        "hidden": bool(defined_by and defined_by.is_hidden),
        "dependencies": list(lineage.dependencies(resolved)) if lineage else [],
        # What breaks if it is deleted. The single most important field here.
        "dependents": list(lineage.dependents(resolved)) if lineage else [],
        "report_bindings": [b.where() for b in bindings_for(model, resolved)],
        # The file to edit. An agent asked to act on a finding needs this more than a
        # person does: a person can search the project, an agent will invent a path.
        "defined_in": where.describe(model.root) if (where := model.where(resolved)) else None,
    }
    if metrics:
        payload["storage"] = {
            "size": _bytes(metrics.total_bytes),
            "bytes": metrics.total_bytes,
            "cardinality": metrics.cardinality,
            "encoding": metrics.encoding,
        }
    if finding:
        payload["verdict"] = str(finding.verdict)
        payload["reason"] = finding.reason
        payload["action"] = finding.action
        payload["checked"] = list(finding.checked)
        payload["blind_spots"] = list(finding.blind_spots)
    return payload


def _trace_lineage(
    session: Session, key: str, direction: str = "both", hops: int | None = None
) -> dict[str, Any]:
    model = session.ensure()
    resolved, suggestions = _resolve(model, key)
    if resolved is None:
        return {"asked_for": key, "found": False, "did_you_mean": suggestions}

    lineage = session.lineage
    payload: dict[str, Any] = {"asked_for": key, "found": True, "key": resolved, "hops": hops}
    if direction in ("both", "up"):
        payload["dependencies"] = list(lineage.dependencies(resolved, hops))
    if direction in ("both", "down"):
        payload["dependents"] = list(lineage.dependents(resolved, hops))
    if lineage.unresolved:
        payload["unresolved_references"] = (
            f"{len(lineage.unresolved)} DAX reference(s) could not be tied to an object, so "
            "edges are missing from this graph"
        )
    return payload


def _report_issues(session: Session, limit: int = 25) -> dict[str, Any]:
    from dax_quax.analysis.report_findings import ALREADY_BROKEN, report_findings, sort_key

    model = session.ensure()
    findings = sorted(report_findings(model), key=sort_key)
    rows = [
        {
            "kind": finding.kind,
            "subject": finding.subject,
            "detail": finding.detail,
            # These two are wrong on screen now. Everything else here is a question.
            "already_broken": finding.kind in ALREADY_BROKEN,
            "file": _relative(finding.file, model.root),
        }
        for finding in findings
    ]
    return {
        "already_broken": sum(1 for row in rows if row["already_broken"]),
        **_page(rows, limit),
    }


def _rescan(session: Session) -> dict[str, Any]:
    session.rescan()
    return {"rescanned": True, "scans": session.scans, "source": session.description}


# -- the registry ----------------------------------------------------------------------


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}}

TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="describe_model",
            summary=(
                "What this model is: object counts, what the scan could see, and what it "
                "could not. Call this first — the warnings and blind spots here qualify "
                "every verdict the other tools return."
            ),
            schema=_NO_ARGS,
            handler=_describe_model,
        ),
        ToolSpec(
            name="search_objects",
            summary=(
                "Find objects whose key contains a substring, largest first. Use this to "
                "turn a name a person used into a key the other tools accept."
            ),
            schema={
                "type": "object",
                "properties": {
                    "query": _string("substring to match against the object key"),
                    "kind": {
                        "type": "string",
                        "enum": ["column", "calc_column", "measure"],
                        "description": "restrict to one kind of object",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["query"],
            },
            handler=_search_objects,
        ),
        ToolSpec(
            name="list_findings",
            summary=(
                "Ranked cost-and-usage findings, filterable by verdict, kind and size. "
                "The headline answer: what costs the most and is referenced the least."
            ),
            schema={
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["REMOVE", "REVIEW", "UNKNOWN", "KEEP"],
                    },
                    "kind": {"type": "string", "enum": ["column", "calc_column", "measure"]},
                    "min_mb": {"type": "number", "default": 0},
                    "limit": {"type": "integer", "default": 25},
                },
            },
            handler=_list_findings,
        ),
        ToolSpec(
            name="explain_object",
            summary=(
                "Everything known about one object: its size, its DAX, what it depends on, "
                "what breaks if it is deleted, which visuals bind it, and why it was given "
                "the verdict it has. Call this before advising on any single object."
            ),
            schema={
                "type": "object",
                "properties": {
                    "key": _string("e.g. 'Sales[Amount]', 'Total Sales' or '[Total Sales]'")
                },
                "required": ["key"],
            },
            handler=_explain_object,
        ),
        ToolSpec(
            name="trace_lineage",
            summary=(
                "Walk the dependency graph from one object. 'up' is what it needs, 'down' "
                "is what needs it — i.e. what breaks if it goes."
            ),
            schema={
                "type": "object",
                "properties": {
                    "key": _string("the object to walk from"),
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "both"],
                        "default": "both",
                    },
                    "hops": {
                        "type": "integer",
                        "description": "stop after this many hops; omit for transitive",
                    },
                },
                "required": ["key"],
            },
            handler=_trace_lineage,
        ),
        ToolSpec(
            name="report_issues",
            summary=(
                "Findings about the reports rather than the model: visuals bound to objects "
                "the model does not have, empty visuals and pages, duplicate bindings, and "
                "objects a single visual keeps alive."
            ),
            schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
            },
            handler=_report_issues,
        ),
        ToolSpec(
            name="rescan",
            summary=(
                "Re-read the source. The model is held between calls, so call this after "
                "the model has been edited in Power BI Desktop."
            ),
            schema=_NO_ARGS,
            handler=_rescan,
        ),
    )
}

#: What is reachable. See the module docstring: a handler in `TOOLS` but not named here
#: cannot be called, so exposing one is two deliberate edits rather than one.
EXPOSED: frozenset[str] = frozenset(
    {
        "describe_model",
        "search_objects",
        "list_findings",
        "explain_object",
        "trace_lineage",
        "report_issues",
        "rescan",
    }
)


def tool_list() -> list[ToolSpec]:
    """The tools this server advertises, in a stable order."""
    return [TOOLS[name] for name in sorted(EXPOSED)]


def _mentions_verdict(payload: Any) -> bool:
    """Whether anything in this payload is a verdict, however deeply it is nested."""
    if isinstance(payload, dict):
        return "verdict" in payload or any(_mentions_verdict(v) for v in payload.values())
    if isinstance(payload, list):
        return any(_mentions_verdict(item) for item in payload)
    return False


def call(session: Session, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a tool and wrap it in the envelope every answer needs.

    The envelope is built here rather than in each handler so that it cannot be left out
    of one. `scanned_at` makes a held snapshot visible as one; the caveat rides along with
    any payload that carries a verdict, whether or not the handler thought about it.
    """
    if name not in EXPOSED:
        raise DaxQuaxError(
            f"unknown tool {name!r}. This server exposes: {', '.join(sorted(EXPOSED))}"
        )

    payload = TOOLS[name].handler(session, **(arguments or {}))
    model = session.ensure()
    scope = scope_of(model)

    envelope: dict[str, Any] = {
        "model": model.name,
        "source": model.source,
        "scanned_at": model.scanned_at.isoformat(),
        "scope": scope.describe(),
    }
    if _mentions_verdict(payload):
        envelope["verdicts_are_defensible"] = scope.trustworthy
        envelope["caveat"] = VERDICT_CAVEAT if scope.trustworthy else _UNTRUSTWORTHY_CAVEAT
    return {**envelope, "result": payload}
