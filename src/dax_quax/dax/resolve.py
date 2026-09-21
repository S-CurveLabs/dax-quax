"""Turn syntactic candidates into real object references against a Model.

DAX identifiers are case-insensitive, so every lookup here goes through a folded index.

Two buckets for things that do not resolve, and the distinction is deliberate:

* **unresolved** — bracket syntax that matched nothing. ``[Foo]`` is unambiguously an object
  reference, so failing to resolve it means an edge was lost. These are reported.
* **ignored** — a bare identifier that matched no table. Almost always an enum constant
  (``MONTH``, ``BOTH``) rather than a mistake, so counting these as failures would bury the
  real ones in noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dax_quax.dax.extract import Candidate, Extraction, extract
from dax_quax.model import Model, ObjectRef

__all__ = ["ModelIndex", "Reference", "Resolution", "Unresolved", "references_in", "resolve"]

Via = Literal["qualified", "measure", "context_column", "table", "table_qualified_measure"]


@dataclass(frozen=True, slots=True)
class Reference:
    ref: ObjectRef
    via: Via
    position: int = 0

    @property
    def key(self) -> str:
        return self.ref.key


@dataclass(frozen=True, slots=True)
class Unresolved:
    text: str
    position: int
    reason: str


@dataclass(slots=True)
class Resolution:
    references: tuple[Reference, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    ignored: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        """Distinct referenced object keys, in first-seen order."""
        seen: dict[str, None] = {}
        for reference in self.references:
            seen.setdefault(reference.key, None)
        return tuple(seen)


class ModelIndex:
    """Case-insensitive lookups over a Model.

    Built once and reused. Lineage resolves every expression in the model, so rebuilding
    this per call would make the graph build quadratic in model size.
    """

    __slots__ = ("_columns", "_measures", "_model", "_tables")

    def __init__(self, model: Model) -> None:
        self._model = model
        self._tables = {name.casefold(): name for name in model.tables}
        self._measures = {m.name.casefold(): m for m in model.measures.values()}
        self._columns: dict[tuple[str, str], str] = {
            (c.table.casefold(), c.name.casefold()): c.key for c in model.columns.values()
        }

    @property
    def model(self) -> Model:
        return self._model

    def table(self, name: str) -> str | None:
        return self._tables.get(name.casefold())

    def measure(self, name: str) -> ObjectRef | None:
        measure = self._measures.get(name.casefold())
        return measure.ref if measure else None

    def column(self, table: str, name: str) -> ObjectRef | None:
        key = self._columns.get((table.casefold(), name.casefold()))
        if key is None:
            return None
        return self._model.columns[key].ref


def resolve(
    extraction: Extraction,
    index: ModelIndex,
    context_table: str | None = None,
) -> Resolution:
    """Resolve candidates against a model.

    ``context_table`` is the home table of the expression — the measure's table, or the
    table a calculated column belongs to. It is what makes an unqualified ``[Amount]``
    resolvable to a column rather than only to a measure.
    """
    references: list[Reference] = []
    unresolved: list[Unresolved] = []
    ignored: list[str] = []
    notes: list[str] = []

    for candidate in extraction.candidates:
        match candidate.kind:
            case "qualified_column":
                _resolve_qualified(candidate, index, references, unresolved, notes)
            case "bare_bracket":
                _resolve_bare_bracket(candidate, index, context_table, references, unresolved)
            case "bare_table":
                table = index.table(candidate.name)
                if table is None:
                    ignored.append(candidate.name)
                else:
                    references.append(
                        Reference(ObjectRef("table", table), "table", candidate.position)
                    )

    return Resolution(
        references=tuple(references),
        unresolved=tuple(unresolved),
        ignored=tuple(dict.fromkeys(ignored)),
        notes=tuple(dict.fromkeys(notes)),
    )


def _resolve_qualified(
    candidate: Candidate,
    index: ModelIndex,
    references: list[Reference],
    unresolved: list[Unresolved],
    notes: list[str],
) -> None:
    assert candidate.table is not None
    column = index.column(candidate.table, candidate.name)
    if column is not None:
        references.append(Reference(column, "qualified", candidate.position))
        return

    # Table-qualified measure references are legal and discouraged. Resolve them rather
    # than losing the edge, but say so — a lineage gap here is a false 'unused' later.
    measure = index.measure(candidate.name)
    if measure is not None:
        references.append(Reference(measure, "table_qualified_measure", candidate.position))
        notes.append(
            f"{candidate} is a table-qualified measure reference; "
            f"resolved to {measure.key}"
        )
        return

    unresolved.append(
        Unresolved(
            str(candidate), candidate.position, "no such column, and no measure of that name"
        )
    )


def _resolve_bare_bracket(
    candidate: Candidate,
    index: ModelIndex,
    context_table: str | None,
    references: list[Reference],
    unresolved: list[Unresolved],
) -> None:
    # Measure names are unique model-wide, so a measure wins over a same-named column of
    # the surrounding table — which is also how the engine resolves it.
    measure = index.measure(candidate.name)
    if measure is not None:
        references.append(Reference(measure, "measure", candidate.position))
        return

    if context_table is not None:
        column = index.column(context_table, candidate.name)
        if column is not None:
            references.append(Reference(column, "context_column", candidate.position))
            return

    reason = (
        "no measure of that name, and no such column on the surrounding table"
        if context_table
        else "no measure of that name, and no surrounding table to look in"
    )
    unresolved.append(Unresolved(str(candidate), candidate.position, reason))


def references_in(
    expression: str | None,
    model: Model,
    context_table: str | None = None,
) -> Resolution:
    """Convenience: extract and resolve in one call.

    Builds a fresh index each time, so use :class:`ModelIndex` directly in a loop.
    """
    return resolve(extract(expression), ModelIndex(model), context_table)
