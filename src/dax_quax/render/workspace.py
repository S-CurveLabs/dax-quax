"""Render a workspace scan: an index, plus one page per model.

The index answers the question a directory raises that a single model does not — *how much
of this can I trust* — and then hands off to the existing per-model page for detail. Those
per-model pages are the same template `report` already writes, given the workspace's scope
so the degrade shows there too rather than only on the index.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from dax_quax.analysis.usage import Thresholds, Verdict, summary
from dax_quax.render.diagram import DIAGRAM_STYLES, render_diagram
from dax_quax.render.editor import DEFAULT_EDITOR
from dax_quax.render.report import TEMPLATES, format_bytes, render_report

if TYPE_CHECKING:
    from dax_quax.sources.workspace import Workspace

__all__ = ["build_workspace_context", "render_workspace", "slug_map", "slugify"]

#: How many findings to preview on a model's card before sending the reader to its page.
PREVIEW = 5
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(name: str) -> str:
    """A filename for a model name. Model names allow spaces and punctuation; paths do not."""
    cleaned = _UNSAFE.sub("-", name).strip("-")
    return (cleaned or "model").lower()


def slug_map(names: Iterable[str]) -> dict[str, str]:
    """Model name -> slug, guaranteed distinct.

    `slugify` folds away case and punctuation, so "Sales EU" and "sales-eu" both land on
    the same slug. Written to a directory that silently overwrites one page with the
    other; served, it silently shows one model under the other's URL. Neither is a thing
    to find out from a reader, so a collision gets a suffix here instead.
    """
    taken: dict[str, str] = {}
    slugs: dict[str, str] = {}
    for name in sorted(names):
        base = slugify(name)
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        taken[slug] = name
        slugs[name] = slug
    return slugs


def _workspace_diagram_html(workspace: Workspace) -> str:
    from dax_quax.analysis.diagram import workspace_diagram

    try:
        return render_diagram(workspace_diagram(workspace), element_id="ws-diagram")
    except Exception as exc:  # noqa: BLE001
        return f'<div class="dg-note">the diagram could not be drawn ({exc})</div>'


def build_workspace_context(
    workspace: Workspace,
    *,
    thresholds: Thresholds | None = None,
    href: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """``href`` turns a model name into a link; the default writes a sibling .html file."""
    slugs = slug_map(workspace.models)
    href = href or (lambda name: f"{slugs[name]}.html")
    trustworthy = not workspace.unmatched
    orphans = set(workspace.orphan_models())

    models: list[dict[str, Any]] = []
    objects = 0
    removable = 0

    for name in sorted(workspace.models):
        findings = workspace.findings_for(name, thresholds=thresholds)
        head = summary(findings)
        objects += head["objects"]
        removable += head["removable_objects"]
        scope = workspace.scope_for(name)

        preview = [f for f in findings if f.is_actionable][:PREVIEW]
        models.append(
            {
                "name": name,
                "href": href(name),
                "scope": scope.describe(),
                "trustworthy": scope.trustworthy,
                "orphan": name in orphans,
                "tally": [
                    (str(verdict), head[key])
                    for verdict, key in (
                        (Verdict.REMOVE, "removable_objects"),
                        (Verdict.REVIEW, "review_objects"),
                        (Verdict.UNKNOWN, "unknown_objects"),
                        (Verdict.KEEP, "keep_objects"),
                    )
                    if head[key]
                ],
                "findings": [
                    {
                        "verdict": str(finding.verdict),
                        "key": finding.key,
                        "size": format_bytes(finding.bytes),
                        "used_by": workspace.reports_using(name, finding.key)[:2],
                    }
                    for finding in preview
                ],
            }
        )

    links = [
        {
            "name": link.name,
            "model": link.model,
            "reason": link.reason,
            "how": (
                f"byPath {link.by_path}"
                if link.by_path
                else (f"byConnection {link.by_connection}" if link.by_connection else "")
            ),
        }
        for link in workspace.links
    ]

    return {
        "title": f"{workspace.root.name} — workspace scan",
        "root": str(workspace.root),
        "coverage": workspace.describe(),
        "trustworthy": trustworthy,
        "model_count": len(workspace.models),
        "report_count": len(workspace.links),
        "matched_count": len(workspace.matched),
        "unmatched": [link.name for link in workspace.unmatched],
        "object_count": objects,
        "removable_objects": removable,
        "orphans": sorted(orphans),
        "models": models,
        "links": links,
        "cross_model": workspace.cross_model,
        "warnings": workspace.warnings,
        "diagram": _workspace_diagram_html(workspace),
        "diagram_styles": DIAGRAM_STYLES,
        # Served mode overrides these; a written directory is never served.
        "served": False,
        "scans": 0,
        "loaded_at": None,
        "load_error": None,
        "extra_blind_spots": [],
        "generated_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


def render_workspace(
    workspace: Workspace,
    out_dir: str | pathlib.Path,
    *,
    thresholds: Thresholds | None = None,
    editor: str = DEFAULT_EDITOR,
) -> pathlib.Path:
    """Write ``index.html`` and one page per model. Returns the index."""
    from jinja2 import Environment, FileSystemLoader

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    slugs = slug_map(workspace.models)
    for name, model in workspace.models.items():
        render_report(
            model,
            out / f"{slugs[name]}.html",
            thresholds=thresholds,
            title=f"{name} — model cost and usage",
            # The workspace knows about reports this model has never seen, so its scope has
            # to travel with it; otherwise the per-model page would claim a confidence the
            # index has already withheld.
            scope=workspace.scope_for(name),
            back=("index.html", "workspace"),
            editor=editor,
        )

    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = environment.get_template("workspace.html.j2").render(
        **build_workspace_context(workspace, thresholds=thresholds)
    )
    index = out / "index.html"
    index.write_text(html, encoding="utf-8")
    return index
