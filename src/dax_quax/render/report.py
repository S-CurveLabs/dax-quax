"""Render findings to a single self-contained HTML file.

One template set serves two delivery modes: this renders it to a file, and M6's ``serve``
will render the same templates over HTTP. There is no second rendering path.

SELF-CONTAINED MEANS NO NETWORK
-------------------------------
The output is meant to survive being emailed, opened on a locked-down machine, or read
months later. So: no CDN, no webfonts, no external anything. Styling uses system font
stacks and the data is embedded as JSON in the page. A report that needs the internet to
render is not a report, it is a bookmark.

Formatting to "214.3 MB" happens here and nowhere else. Everything upstream carries ints.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from typing import TYPE_CHECKING, Any

from dax_quax.analysis.usage import (
    Finding,
    ScanScope,
    Thresholds,
    assess,
    scope_of,
    summary,
)
from dax_quax.model import Model
from dax_quax.render._escape import script_safe_json
from dax_quax.render.diagram import DIAGRAM_STYLES, render_diagram
from dax_quax.render.editor import DEFAULT_EDITOR, editor_uri
from dax_quax.report import Binding, bindings_for

if TYPE_CHECKING:
    from dax_quax.analysis.lineage import LineageGraph

__all__ = ["build_context", "format_bytes", "render_report"]

TEMPLATES = pathlib.Path(__file__).parent / "templates"
_KB = 1024
_MB = _KB * 1024
_GB = _MB * 1024


def format_bytes(size: int | None) -> str:
    """Never returns '0 B' for something unmeasured — that distinction is the point."""
    if size is None:
        return "—"
    if size >= _GB:
        return f"{size / _GB:.2f} GB"
    if size >= _MB:
        return f"{size / _MB:.1f} MB"
    if size >= _KB:
        return f"{size / _KB:.1f} KB"
    return f"{size} B"


def _model_diagram_html(model: Model) -> str:
    """The model's own diagram. A failure here must not cost you the whole report."""
    from dax_quax.analysis.diagram import model_diagram

    try:
        return render_diagram(model_diagram(model), element_id="model-diagram")
    except Exception as exc:  # noqa: BLE001 - a picture is not worth the page
        return f'<div class="dg-note">the diagram could not be drawn ({exc})</div>'


def _report_findings_rows(model: Model, editor: str = DEFAULT_EDITOR) -> list[dict[str, Any]]:
    """Findings about the report layer, with what is already broken at the top.

    These invert the rest of the page. Everything above answers "can I delete this"; these
    answer "what is broken right now", which is the more urgent of the two questions.
    """
    from dax_quax.analysis.report_findings import ALREADY_BROKEN, report_findings, sort_key

    def _ref(path):  # noqa: ANN001, ANN202 - a two-line adapter
        from dax_quax.model import SourceRef

        return SourceRef(path=path) if path else None

    return [
        {
            "kind": finding.kind,
            "subject": finding.subject,
            "detail": finding.detail,
            "broken": finding.kind in ALREADY_BROKEN,
            "file": _relative(finding.file, model.root),
            "uri": editor_uri(_ref(finding.file), editor),
        }
        for finding in sorted(report_findings(model), key=sort_key)
    ]


def _format_count(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def build_context(
    model: Model,
    *,
    lineage: LineageGraph | None = None,
    thresholds: Thresholds | None = None,
    title: str | None = None,
    scope: ScanScope | None = None,
    back: tuple[str, str] | None = None,
    editor: str = DEFAULT_EDITOR,
) -> dict[str, Any]:
    """Everything the template needs, already formatted. No model objects leak through.

    ``scope`` is passed in by a workspace scan, which knows about reports this model has
    never seen. ``back`` is (href, label) for a link to a workspace index.
    """
    from dax_quax.analysis.lineage import build_lineage

    lineage = lineage if lineage is not None else build_lineage(model)
    scope = scope if scope is not None else scope_of(model)
    findings = assess(model, lineage=lineage, thresholds=thresholds, scope=scope)
    head = summary(findings)
    report_scanned = scope.any_report
    has_metrics = model.has("metrics")

    rows = [_row(f, model, lineage, report_scanned, editor) for f in findings]
    largest = max((f.bytes or 0 for f in findings), default=0)
    for row in rows:
        raw = row["bytes_raw"]
        row["bar_pct"] = (raw / largest * 100) if largest and raw else 0.0

    warnings = list(model.warnings)
    for report in model.reports:
        warnings += list(report.warnings)
        if report.unparsed:
            warnings.append(
                f"{report.name}: {len(report.unparsed)} field reference(s) could not be "
                "resolved to a table; objects used only there may look unreferenced"
            )
    if lineage.unresolved:
        warnings.append(
            f"{len(lineage.unresolved)} DAX reference(s) could not be resolved to a model "
            "object; edges are missing from the lineage graph"
        )

    return {
        "title": title or f"{model.name} — model cost and usage",
        "model_name": model.name,
        "source": model.source,
        "scanned_at": model.scanned_at.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "has_metrics": has_metrics,
        "report_findings": _report_findings_rows(model, editor),
        "has_locations": model.has("locations"),
        # Served mode overrides these; a written file is never served.
        "diagram": _model_diagram_html(model),
        "diagram_styles": DIAGRAM_STYLES,
        # Where this page's own API lives, relative or absolute. Served modes override it.
        "api_base": "api/",
        "served": False,
        "loaded_at": None,
        "load_error": None,
        "report_scanned": report_scanned,
        "report_names": [r.name for r in model.reports],
        "report_pages": sum(len(r.pages) for r in model.reports),
        "report_visuals": sum(r.visual_count for r in model.reports),
        # The coverage statement. A verdict is only as good as its scope, so this is a
        # headline rather than something the reader has to infer.
        "scope": scope.describe(),
        "scope_trustworthy": scope.trustworthy,
        "back": back,
        "counts": {
            "tables": len(model.tables),
            "columns": sum(1 for c in model.columns.values() if not c.is_row_number),
            "measures": len(model.measures),
        },
        "summary": {
            **head,
            "measured_bytes_h": format_bytes(head["measured_bytes"]) if has_metrics else "—",
            "removable_bytes_h": format_bytes(head["removable_bytes"]) if has_metrics else "—",
            "review_bytes_h": format_bytes(head["review_bytes"]) if has_metrics else "—",
        },
        "rows": rows,
        "rows_json": script_safe_json(rows),
        "blind_spots": list(findings[0].blind_spots) if findings else [],
        "warnings": warnings,
        "verdicts": ["REMOVE", "REVIEW", "UNKNOWN", "KEEP"],
        "verdict_counts": {
            "REMOVE": head["removable_objects"],
            "REVIEW": head["review_objects"],
            "UNKNOWN": head["unknown_objects"],
            "KEEP": head["keep_objects"],
        },
    }


def _row(
    finding: Finding,
    model: Model,
    lineage: LineageGraph,
    report_scanned: bool,
    editor: str = DEFAULT_EDITOR,
) -> dict[str, Any]:
    column = model.columns.get(finding.key)
    metrics = column.metrics if column else None
    bindings = bindings_for(model, finding.key) if report_scanned else ()
    where = model.where(finding.key)

    return {
        "key": finding.key,
        "table": finding.table,
        "name": finding.name,
        "kind": finding.kind,
        "kind_label": {"column": "COL", "calc_column": "FX", "measure": "MSR"}[finding.kind],
        "hidden": finding.hidden,
        "verdict": str(finding.verdict),
        "bytes_raw": finding.bytes or 0,
        "bytes_h": format_bytes(finding.bytes),
        "measured": finding.bytes is not None,
        "pct": f"{finding.pct_of_model:.1f}%" if finding.pct_of_model is not None else "—",
        "cardinality": _format_count(metrics.cardinality if metrics else None),
        "encoding": (metrics.encoding if metrics else None) or "—",
        "model_refs": finding.model_refs,
        "report_bindings": finding.report_bindings,
        "reason": finding.reason,
        "action": finding.action,
        "checked": list(finding.checked),
        "expression": (
            model.measures[finding.key].expression
            if finding.key in model.measures
            else (column.expression if column else None)
        ),
        "breakdown": (
            [
                {"label": "Dictionary", "value": format_bytes(metrics.dictionary_bytes)},
                {"label": "Data (segments)", "value": format_bytes(metrics.data_bytes)},
                {"label": "Hierarchies", "value": format_bytes(metrics.hierarchy_bytes)},
            ]
            if metrics
            else []
        ),
        "upstream": list(lineage.dependencies(finding.key, hops=1)),
        "downstream": list(lineage.dependents(finding.key, hops=1)),
        "bindings": [
            {
                "where": b.where(),
                "location": b.location,
                "file": _relative(b.file, model.root),
                "uri": _binding_uri(b, editor),
            }
            for b in bindings
        ],
        # Where to go and change it. Absent when the source could not know, never guessed.
        "defined_in": where.describe(model.root) if where else None,
        "defined_uri": editor_uri(where, editor),
    }


def _relative(path: pathlib.Path | None, root: pathlib.Path | None) -> str | None:
    """The path as a reader would say it. The URI beside it keeps the absolute one."""
    if path is None:
        return None
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _binding_uri(binding: Binding, editor: str) -> str | None:
    """A visual's own .json. PBIR is one file per visual, so there is no line to give."""
    from dax_quax.model import SourceRef

    return editor_uri(SourceRef(path=binding.file), editor) if binding.file else None


def render_report(
    model: Model,
    out_path: str | pathlib.Path,
    *,
    lineage: LineageGraph | None = None,
    thresholds: Thresholds | None = None,
    title: str | None = None,
    scope: ScanScope | None = None,
    back: tuple[str, str] | None = None,
    editor: str = DEFAULT_EDITOR,
) -> pathlib.Path:
    """Write the report and return where it landed."""
    from jinja2 import Environment, FileSystemLoader

    # autoescape=True, not select_autoescape(["html"]): that helper matches on the
    # filename suffix, and "report.html.j2" ends in .j2, so it silently leaves escaping
    # OFF for every template here. Everything this module renders is HTML.
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = environment.get_template("report.html.j2").render(
        **build_context(
            model,
            lineage=lineage,
            thresholds=thresholds,
            title=title,
            scope=scope,
            back=back,
            editor=editor,
        )
    )
    path = pathlib.Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
