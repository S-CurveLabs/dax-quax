"""Scan a directory of .pbip artefacts and assess each model against every report over it.

WHY THIS EXISTS
---------------
A verdict built from one report answers "does *this* report use it". A semantic model
normally has several thin reports over it, so a column one report ignores can be
load-bearing in another. Assessing against a single report is the most dangerous thing
this tool can do, and a directory scan is the fix. See CONVENTIONS §14.

WHAT IT STILL CANNOT SEE
------------------------
It upgrades "one report scanned" to "every report in this directory scanned". Not to
certainty. `.pbip` is source, not deployment, so a report living only in a workspace is
invisible, as is every non-.pbip consumer.

THE ASYMMETRY
-------------
The two ways of getting the match wrong are not equally bad:

    a report not matched to its model  ->  uncounted usage  ->  false REMOVE  ->  someone
                                           deletes a live object
    a model not matched to a report    ->  harmless; the bindings find no home

So an unmatched report degrades **every** model in the scan, not just the one it might have
belonged to — the whole problem is that we do not know which one that is.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

from dax_quax.analysis.usage import Finding, ScanScope, Thresholds, assess
from dax_quax.model import Model
from dax_quax.report import ReportBindings, attach_report, load_report
from dax_quax.sources.pbip import load_model

__all__ = [
    "parse_connection_string","ReportLink", "Workspace", "discover_workspace"]

_MODEL_SUFFIXES = (".SemanticModel", ".Dataset")

#: Keys inside a byConnection connection string. Verified 2026-09-18 against the published
#: PBIR schema (definitionProperties 2.0.0), where `byConnection` has **only**
#: `connectionString` and `additionalProperties: false`.
#:
#: The previous contract guessed at five sibling fields and every one of them was wrong:
#: none exist in the 2.0.0 shape at all, so a modern byConnection report matched nothing
#: and was reported as naming neither a path nor a connection. In the older 1.0.0 shape
#: the fields do exist, but `pbiModelDatabaseName` holds the semantic model **id**, not
#: its name, and `name` is the constant "EntityDataSource" -- so matching a model name
#: against either could only ever succeed by accident.
#:
#: The dataset's name lives inside the connection string, as `initial catalog`.
_CATALOG_KEYS = ("initial catalog", "catalog")
_MODEL_ID_KEYS = ("semanticmodelid", "datasetid")
_DATA_SOURCE_KEY = "data source"

#: Fields of the older 1.0.0 byConnection shape. Kept because a report written by an
#: earlier Desktop is still on disk somewhere; `pbiModelDatabaseName` is an id.
_LEGACY_ID_FIELDS = ("pbiModelDatabaseName", "datasetId")


def parse_connection_string(text: str) -> dict[str, str]:
    """Split an XMLA connection string into case-folded keys.

    Quote-aware: the 2.0.0 form quotes its data source
    (`Data Source="powerbi://.../myorg/Sales";initial catalog=...`), and a workspace name
    may contain a semicolon, so splitting on ';' alone would cut a value in half.
    """
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ";":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))

    found: dict[str, str] = {}
    for part in parts:
        key, sep, value = part.partition("=")
        if sep and key.strip():
            found[key.strip().casefold()] = value.strip()
    return found


def _connection_target(connection: dict) -> tuple[str | None, str | None, str | None]:
    """(dataset name, dataset id, workspace) out of a byConnection reference."""
    parsed = parse_connection_string(str(connection.get("connectionString") or ""))

    name = next((parsed[k] for k in _CATALOG_KEYS if parsed.get(k)), None)
    identifier = next((parsed[k] for k in _MODEL_ID_KEYS if parsed.get(k)), None)
    if identifier is None:
        identifier = next(
            (str(connection[f]) for f in _LEGACY_ID_FIELDS if connection.get(f)), None
        )

    workspace = None
    source = parsed.get(_DATA_SOURCE_KEY)
    if source and "/" in source:
        workspace = source.rstrip("/").rsplit("/", 1)[-1] or None
    return name, identifier, workspace

#: M source text that means this model reads another model. A heuristic, and declared as
#: one: a missed edge understates coupling, it does not invent any.
_CROSS_MODEL = re.compile(
    r"AnalysisServices\.Database|PowerBI\.Datasets|PowerPlatform\.Dataflows", re.IGNORECASE
)


@dataclass(slots=True)
class ReportLink:
    """One report, and the model it claims to point at."""

    path: pathlib.Path
    name: str
    by_path: str | None = None
    by_connection: str | None = None
    #: The Fabric workspace a byConnection points into, when its connection string says.
    workspace: str | None = None
    #: The semantic model id, when the connection string or a legacy field carries one.
    model_id: str | None = None
    model: str | None = None
    reason: str | None = None

    @property
    def matched(self) -> bool:
        return self.model is not None

    def describe(self) -> str:
        target = self.by_path or self.by_connection or "nothing"
        return f"{self.name} -> {target}"


@dataclass(slots=True)
class Workspace:
    root: pathlib.Path
    models: dict[str, Model] = field(default_factory=dict)
    links: list[ReportLink] = field(default_factory=list)
    model_paths: dict[str, pathlib.Path] = field(default_factory=dict)
    cross_model: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- coverage -----------------------------------------------------------------------

    @property
    def unmatched(self) -> list[ReportLink]:
        return [link for link in self.links if not link.matched]

    @property
    def matched(self) -> list[ReportLink]:
        return [link for link in self.links if link.matched]

    def reports_for(self, model: str) -> list[ReportLink]:
        return [link for link in self.links if link.model == model]

    def orphan_models(self) -> list[str]:
        """Models no report in this directory references.

        Often a bigger win than any single column, and invisible to single-model tooling.
        """
        used = {link.model for link in self.links if link.model}
        # cross_model edges are (reader, target). The model being *read* is the one that
        # is in use; excluding the reader instead would report a model another model
        # depends on as unused, which is the error this whole tool exists to avoid.
        used |= {target for _, target in self.cross_model if target}
        return sorted(name for name in self.models if name not in used)

    def scope_for(self, model: str) -> ScanScope:
        # Counted from the bindings actually attached rather than from the links, because
        # a matched report whose bindings could not be read is uncounted usage in exactly
        # the way an unmatched one is. See ScanScope.reports_unreadable.
        attached = self.models[model].reports if model in self.models else []
        readable = [report for report in attached if report.readable]
        return ScanScope(
            model_source="pbip",
            reports_scanned=len(readable),
            report_names=tuple(report.name for report in readable),
            # Workspace-wide: an unmatched report could belong to any model here.
            reports_unmatched=len(self.unmatched),
            reports_unreadable=len(attached) - len(readable),
        )

    def describe(self) -> str:
        text = (
            f"{len(self.models)} model(s), {len(self.links)} report(s), "
            f"{len(self.matched)} matched"
        )
        if self.unmatched:
            text += f", {len(self.unmatched)} unmatched"
        return text

    # -- analysis ------------------------------------------------------------------------

    def findings_for(self, model: str, *, thresholds: Thresholds | None = None) -> list[Finding]:
        return assess(self.models[model], thresholds=thresholds, scope=self.scope_for(model))

    def reports_using(self, model: str, key: str) -> list[str]:
        """Which reports would break if ``key`` were deleted, named down to the visual.

        A count is not enough to hand to someone else; a list is.
        """
        loaded = self.models.get(model)
        if loaded is None:
            return []
        # Binding.where() names its own report, so it is not prefixed again here.
        where = [b.where() for report in loaded.reports for b in report.for_key(key)]
        return sorted(dict.fromkeys(where))


def discover_workspace(
    root: str | pathlib.Path, *, load: bool = True
) -> Workspace:
    """Find every .pbip artefact under ``root`` and wire the reports to their models."""
    root = pathlib.Path(root)
    if not root.is_dir():
        from dax_quax.errors import DaxQuaxError

        raise DaxQuaxError(f"not a directory: {root}")

    workspace = Workspace(root=root)

    model_dirs = sorted(
        path
        for path in root.rglob("*")
        if path.is_dir() and path.name.endswith(_MODEL_SUFFIXES)
    )
    for path in model_dirs:
        name = path.name
        for suffix in _MODEL_SUFFIXES:
            name = name.removesuffix(suffix)
        if name in workspace.model_paths:
            workspace.warnings.append(
                f"two semantic models are both called {name!r} "
                f"({workspace.model_paths[name]} and {path}); reports cannot be told apart"
            )
            continue
        workspace.model_paths[name] = path

    for path in sorted(p for p in root.rglob("*.Report") if p.is_dir()):
        workspace.links.append(_link(path, workspace))

    if load:
        _load_models(workspace)
    return workspace


def _link(path: pathlib.Path, workspace: Workspace) -> ReportLink:
    name = path.name
    link = ReportLink(path=path, name=name)
    pbir = path / "definition.pbir"

    if not pbir.is_file():
        link.reason = "no definition.pbir, so its model cannot be identified"
        return link
    try:
        reference = json.loads(pbir.read_text(encoding="utf-8")).get("datasetReference") or {}
    except (OSError, json.JSONDecodeError) as exc:
        link.reason = f"definition.pbir could not be read ({exc})"
        return link

    by_path = (reference.get("byPath") or {}).get("path")
    if by_path:
        link.by_path = str(by_path)
        target = (path / by_path).resolve()
        for model_name, model_path in workspace.model_paths.items():
            if model_path.resolve() == target:
                link.model = model_name
                return link
        link.reason = f"byPath points at {by_path!r}, which is not a model in this scan"
        return link

    connection = reference.get("byConnection") or {}
    if connection:
        named, identifier, remote = _connection_target(connection)
        link.by_connection = named or identifier or "an unnamed connection"
        link.workspace = remote
        link.model_id = identifier

        if named:
            folded = {key.casefold(): key for key in workspace.model_paths}
            if named.casefold() in folded:
                # A name match across a boundary, not a path match. If it is wrong the
                # report's usage is credited to the wrong model, which makes objects look
                # more used rather than less -- the safe direction. Being wrong the other
                # way, by not matching, degrades every verdict, which is also safe.
                link.model = folded[named.casefold()]
                return link
            link.reason = (
                f"byConnection names the dataset {named!r}"
                + (f" in workspace {remote!r}" if remote else "")
                + ", which is not a model in this scan. It may be published only, or renamed."
            )
            return link

        link.reason = (
            "byConnection carries no dataset name. Its connection string has no "
            "'initial catalog', so the model it points at cannot be named"
            + (f" (id {identifier})" if identifier else "")
            + ". A report deployed through the Fabric REST API is written this way."
        )
        return link

    link.reason = "definition.pbir names neither a path nor a connection"
    return link


def _load_models(workspace: Workspace) -> None:
    for name, path in workspace.model_paths.items():
        try:
            model = load_model(path)
        except Exception as exc:  # noqa: BLE001 - one bad model must not stop the scan
            workspace.warnings.append(f"{name}: could not load ({type(exc).__name__}: {exc})")
            continue
        workspace.models[name] = model
        _find_cross_model_edges(name, model, workspace)

    for link in workspace.matched:
        model = workspace.models.get(link.model or "")
        if model is None:
            continue
        try:
            bindings: ReportBindings = load_report(link.path)
        except Exception as exc:  # noqa: BLE001
            workspace.warnings.append(f"{link.name}: could not read ({exc})")
            link.model = None
            link.reason = f"report could not be read ({exc})"
            continue
        attach_report(model, bindings)

    if workspace.unmatched:
        workspace.warnings.append(
            f"{len(workspace.unmatched)} report(s) could not be matched to a model, so their "
            "usage is not counted. Every REMOVE has been downgraded to UNKNOWN: "
            + "; ".join(f"{link.name} ({link.reason})" for link in workspace.unmatched)
        )


def _find_cross_model_edges(name: str, model: Model, workspace: Workspace) -> None:
    """Record models that read another model, from their partition source text.

    A heuristic over M, and declared as one. It looks for the connectors that reach another
    semantic model, then for any model name in this scan appearing in the same expression.
    A missed edge understates coupling; it never invents one. When the connector is there
    but no known model is named, the edge is recorded with an empty target rather than a
    guessed one, because the target may simply be published elsewhere.
    """
    for table in model.tables.values():
        source = table.source or ""
        if not _CROSS_MODEL.search(source):
            continue
        targets = [
            other
            for other in workspace.model_paths
            if other != name and other.casefold() in source.casefold()
        ]
        for target in targets or [""]:
            edge = (name, target)
            if edge not in workspace.cross_model:
                workspace.cross_model.append(edge)
        if not targets:
            workspace.warnings.append(
                f"{name}.{table.name} reads another semantic model, but the target is not a "
                "model in this scan; its coupling is recorded without a target"
            )
