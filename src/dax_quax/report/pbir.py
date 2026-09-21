"""Read field bindings out of a PBIR report folder.

WHY THIS WALKS THE WHOLE JSON
-----------------------------
The obvious implementation reads known paths — ``visual.query.queryState.*.projections``
— and it is wrong. Field references also live in filter configs, in sort definitions, in
bookmark state, and buried inside ``objects`` where conditional formatting and data bars
keep them. A path whitelist misses those, and every miss becomes a column reported as safe
to delete when a visual is quietly colouring itself by it.

So this walks every node of every JSON file and collects references *by shape* instead:

    {"Column":  {"Expression": {"SourceRef": {"Entity": "Product"}}, "Property": "Color"}}
    {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}},   "Property": "Total Sales"}}

An aggregation wraps a column, a hierarchy level wraps a hierarchy — the walk finds the
inner reference in both cases without needing to know the wrapper.

Anything shaped like a reference but missing its entity is recorded in ``unparsed`` rather
than dropped, because a silent miss here is the expensive kind of wrong.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Literal

__all__ = [
    "NON_DATA_VISUALS",
    "Binding",
    "BindingKind",
    "Location",
    "ReportBindings",
    "Visual",
    "load_report",
]

#: Visual types that carry no data by design. A text box with no fields is a text box;
#: a bar chart with no fields is a broken bar chart, and only the second is a finding.
NON_DATA_VISUALS = frozenset(
    {
        "textbox", "image", "shape", "basicShape", "actionButton", "button",
        "bookmarkNavigator", "pageNavigator", "navigationButton", "blank",
    }
)

BindingKind = Literal["column", "measure", "hierarchy_level"]
Location = Literal[
    "field-well", "filter", "conditional-format", "sort", "bookmark", "drillthrough", "other"
]

#: JSON keys that introduce a field reference, mapped to what kind of object it is.
_FIELD_KEYS: dict[str, BindingKind] = {
    "Column": "column",
    "Measure": "measure",
    "HierarchyLevel": "hierarchy_level",
}

#: Ancestor keys that tell us *where* in a visual a reference was found. Checked from the
#: deepest ancestor outward, so the most specific wins.
_LOCATION_MARKERS: tuple[tuple[str, Location], ...] = (
    ("objects", "conditional-format"),
    ("visualContainerObjects", "conditional-format"),
    ("filterConfig", "filter"),
    ("filters", "filter"),
    ("sortDefinition", "sort"),
    ("sort", "sort"),
    ("drillFilterOtherVisuals", "drillthrough"),
    ("queryState", "field-well"),
    ("query", "field-well"),
)


@dataclass(frozen=True, slots=True)
class Binding:
    """One reference to a model object from somewhere in a report."""

    entity: str
    property: str
    kind: BindingKind
    location: Location
    page: str | None = None
    visual: str | None = None
    visual_type: str | None = None
    report: str | None = None
    #: The .json this was read from. PBIR puts one visual in one file, so a path with no
    #: line number is not a partial answer here, it is the whole one.
    file: pathlib.Path | None = None

    @property
    def key(self) -> str:
        """The model key this binding probably names. Resolution confirms it."""
        return f"[{self.property}]" if self.kind == "measure" else f"{self.entity}[{self.property}]"

    def where(self) -> str:
        parts = [p for p in (self.report, self.page, self.visual_type or self.visual) if p]
        return f"{' / '.join(parts)} ({self.location})" if parts else self.location


@dataclass(frozen=True, slots=True)
class Visual:
    """One visual, recorded whether or not it binds anything.

    A visual with no bindings cannot be found by looking at bindings, which is exactly the
    thing worth finding.
    """

    page: str
    name: str
    visual_type: str | None = None
    file: pathlib.Path | None = None

    @property
    def carries_data(self) -> bool:
        return (self.visual_type or "") not in NON_DATA_VISUALS

    def where(self) -> str:
        return f"{self.page} / {self.visual_type or self.name}"


@dataclass(slots=True)
class ReportBindings:
    name: str
    bindings: list[Binding] = field(default_factory=list)
    pages: tuple[str, ...] = ()
    visuals: list[Visual] = field(default_factory=list)
    #: Page name -> its page.json. The file to open for a finding about a whole page.
    page_files: dict[str, pathlib.Path] = field(default_factory=dict)
    visual_count: int = 0
    warnings: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    #: Whether the bindings in this object are the report's bindings, or an empty list
    #: standing in for a report that could not be read at all (the legacy single-file
    #: format, or a folder with no definition/). The difference is the whole ballgame:
    #: "this report binds nothing" licenses REMOVE, "this report was unreadable" must not.
    #: See ScanScope.reports_unreadable.
    readable: bool = True

    def for_key(self, key: str) -> tuple[Binding, ...]:
        folded = key.casefold()
        return tuple(b for b in self.bindings if b.key.casefold() == folded)

    def keys(self) -> set[str]:
        return {b.key.casefold() for b in self.bindings}

    def __repr__(self) -> str:
        return (
            f"<ReportBindings {self.name!r} pages={len(self.pages)} "
            f"visuals={self.visual_count} bindings={len(self.bindings)} "
            f"unparsed={len(self.unparsed)}>"
        )


def load_report(path: str | pathlib.Path) -> ReportBindings:
    """Load bindings from a ``.Report`` folder, or a ``.pbip`` folder containing one."""
    root = _find_report_root(pathlib.Path(path))
    report = ReportBindings(name=root.name)

    definition = root / "definition"
    if not definition.is_dir():
        # Nothing was read, so this is not a report that binds nothing — it is a report
        # whose bindings are unknown. Counting it as scanned would make every column it
        # uses look unreferenced and turn the whole model into REMOVE verdicts.
        report.readable = False
        if (root / "report.json").is_file():
            report.warnings.append(
                f"{root.name} uses the legacy single-file report format. Field bindings "
                "cannot be read from it; enable the PBIR enhanced report format in Power BI "
                "Desktop (Preview features > Power BI Project (.pbip) report format)."
            )
        else:
            report.warnings.append(f"no definition/ folder under {root}")
        return report

    pages: list[str] = []
    visuals = 0

    for page_dir in sorted(p for p in (definition / "pages").glob("*") if p.is_dir()):
        page = page_dir.name
        pages.append(page)
        report.page_files[page] = (page_dir / "page.json").resolve()
        _collect_file(page_dir / "page.json", report, page=page, default="filter")
        for visual_dir in sorted(v for v in (page_dir / "visuals").glob("*") if v.is_dir()):
            visuals += 1
            _collect_file(
                visual_dir / "visual.json", report, page=page, visual=visual_dir.name
            )

    _collect_file(definition / "report.json", report, default="filter")
    for bookmark in sorted((definition / "bookmarks").glob("*.json")):
        # Everything in a bookmark file is bookmark state. Its internal structure mirrors
        # a visual's (filters, query), so the file scope has to win over path markers.
        _collect_file(bookmark, report, default="bookmark", authoritative=True)

    report.pages = tuple(pages)
    report.visual_count = visuals
    return report


def _find_report_root(path: pathlib.Path) -> pathlib.Path:
    if path.is_dir() and path.name.endswith(".Report"):
        return path
    if path.is_dir():
        candidates = sorted(path.glob("*.Report"))
        if candidates:
            return candidates[0]
    if path.is_file() and path.suffix == ".pbip":
        candidates = sorted(path.parent.glob("*.Report"))
        if candidates:
            return candidates[0]
    return path


def _collect_file(
    path: pathlib.Path,
    report: ReportBindings,
    *,
    page: str | None = None,
    visual: str | None = None,
    default: Location = "other",
    authoritative: bool = False,
) -> None:
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.warnings.append(f"could not read {path.name}: {exc}")
        return

    visual_type = None
    if isinstance(document, dict):
        visual_block = document.get("visual")
        if isinstance(visual_block, dict):
            visual_type = visual_block.get("visualType")
    if visual is not None and page is not None:
        report.visuals.append(
            Visual(page=page, name=visual, visual_type=visual_type, file=path.resolve())
        )

    found = list(_walk(document, ()))
    for reference, trail, scope in found:
        binding = _binding(
            reference, trail, default, page, visual, visual_type, authoritative, scope
        )
        if binding is not None:
            binding = replace(binding, report=report.name, file=path.resolve())
        if binding is None:
            report.unparsed.append(f"{path.name}: {'/'.join(trail) or '<root>'}")
            continue
        report.bindings.append(binding)


def _aliases(node: Any) -> dict[str, str]:
    """The table aliases a ``From`` clause declares: [{"Name": "c", "Entity": "Calendar"}]."""
    if not isinstance(node, dict) or not isinstance(node.get("From"), list):
        return {}
    return {
        item["Name"]: item["Entity"]
        for item in node["From"]
        if isinstance(item, dict) and item.get("Name") and item.get("Entity")
    }


def _walk(
    node: Any, trail: tuple[str, ...], scope: dict[str, str] | None = None
) -> Iterator[tuple[tuple[str, Any], tuple[str, ...], dict[str, str]]]:
    """Yield every field-reference-shaped node, its key trail, and the aliases in scope.

    The scope matters: inside a query, a reference names its table through an alias that
    only the enclosing ``From`` clause defines. Carrying the nearest one down means a
    filter written as ``{"SourceRef": {"Source": "c"}}`` resolves to Calendar instead of
    being recorded as unreadable — which is a report binding silently not counted.
    """
    scope = scope or {}
    if isinstance(node, dict):
        here = _aliases(node)
        inner = {**scope, **here} if here else scope
        for key, value in node.items():
            if key in _FIELD_KEYS and isinstance(value, dict):
                yield (key, value), trail, inner
            yield from _walk(value, (*trail, key), inner)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk(item, (*trail, str(index)), scope)


def _binding(
    reference: tuple[str, Any],
    trail: tuple[str, ...],
    default: Location,
    page: str | None,
    visual: str | None,
    visual_type: str | None,
    authoritative: bool = False,
    scope: dict[str, str] | None = None,
) -> Binding | None:
    key, body = reference
    kind = _FIELD_KEYS[key]

    if kind == "hierarchy_level":
        hierarchy = _get(body, "Expression", "Hierarchy")
        variation = _get(hierarchy or {}, "Expression", "PropertyVariationSource")
        if variation:
            # A date hierarchy hanging off a column: Orders[Shipped Date] -> Date
            # Hierarchy -> Month. The hierarchy belongs to the generated date table, but
            # what keeps a model object alive is the base column, so that is the binding.
            entity = _entity(variation, scope)
            prop = variation.get("Property")
            kind = "column"
        else:
            entity = _entity(hierarchy or {}, scope)
            prop = body.get("Level")
    else:
        entity = _entity(body, scope)
        prop = body.get("Property")

    if not entity or not prop:
        return None

    return Binding(
        entity=str(entity),
        property=str(prop),
        kind=kind,
        location=default if authoritative else _location(trail, default),
        page=page,
        visual=visual,
        visual_type=visual_type,
    )


def _entity(body: dict, scope: dict[str, str] | None = None) -> str | None:
    """The table a reference names, directly or through an alias.

    A SourceRef either names the table outright or uses a "Source" alias that the
    enclosing query's From clause defines. An alias with no matching From entry stays
    unresolved and the reference is recorded as unreadable, because guessing which table
    "c" meant is how a column that is still in use gets reported as removable.
    """
    source = _get(body, "Expression", "SourceRef")
    if not isinstance(source, dict):
        return None
    entity = source.get("Entity")
    if entity:
        return str(entity)
    alias = source.get("Source")
    return (scope or {}).get(str(alias)) if alias else None


def _get(body: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(body, dict):
            return None
        body = body.get(key)
    return body


def _location(trail: tuple[str, ...], default: Location) -> Location:
    for key in reversed(trail):
        for marker, location in _LOCATION_MARKERS:
            if key == marker:
                return location
    return default
