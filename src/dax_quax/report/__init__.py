"""The report layer. Reads field bindings out of a .Report folder.

Deliberately knows nothing about the semantic model: it reports what the report *says*, and
analysis/usage.py is what ties those names to real objects. That separation is what lets a
report folder be paired with a model loaded from anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dax_quax.report.pbir import Binding, ReportBindings, Visual, load_report

if TYPE_CHECKING:
    from dax_quax.model import Model

__all__ = [
    "Binding",
    "ReportBindings",
    "Visual",
    "attach_report",
    "bindings_for",
    "load_report",
]


def attach_report(model: Model, *bindings: ReportBindings) -> Model:
    """Pair one or more reports with a model, granting the 'report' capability.

    Variadic because a semantic model usually has several reports over it. Assessing
    against a subset is safe only in the direction of caution: a report you did not pass
    is usage you did not count, which produces false REMOVE verdicts. See CONVENTIONS §14.

    Without any report, every unused verdict is UNKNOWN rather than REMOVE — a model on
    its own cannot tell you whether a visual uses a column.

    A report that could not be read carries its explanation up to the model, because the
    model's warnings are what the CLI prints and a silent unreadable report is how someone
    ends up wondering why every verdict came back UNKNOWN.
    """
    if not bindings:
        return model
    model.reports.extend(bindings)
    for report in bindings:
        if not report.readable:
            model.warnings.extend(report.warnings)
    model.capabilities = frozenset(model.capabilities | {"report"})
    return model


def bindings_for(model: Model, key: str) -> tuple[Binding, ...]:
    """Every binding to ``key`` across every report attached to the model."""
    found: list[Binding] = []
    for report in model.reports:
        found.extend(report.for_key(key))
    return tuple(found)
