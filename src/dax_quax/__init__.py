"""dax-quax — what does every object in this tabular model cost, and can it be deleted?

Public surface. Everything else is internal and may move.
"""

from __future__ import annotations

from dax_quax.analysis.usage import (
    Finding,
    ScanScope,
    Thresholds,
    Verdict,
    assess,
)
from dax_quax.errors import (
    ConnectionDiscoveryError,
    ContractViolation,
    DaxQuaxError,
    MissingCapability,
)
from dax_quax.model import Column, ColumnMetrics, Measure, Model, ObjectRef, Relationship, Table

__version__ = "0.1.0"


def connect(port: int | None = None, database: str | None = None) -> Model:
    """Load a Model from a running local Power BI Desktop instance.

    Windows only, and imported lazily so the rest of the package stays cross-platform.
    """
    from dax_quax.sources.live import connect as _connect

    return _connect(port=port, database=database)


def open_workspace(path: str):
    """Scan a directory of .pbip artefacts: every model, against every report over it."""
    from dax_quax.sources.workspace import discover_workspace

    return discover_workspace(path)


def open_pbip(path: str, *, attach_reports: bool = True) -> Model:
    """Load a Model from a .pbip project folder, with its sibling reports attached.

    Cross-platform: no .NET, no Power BI Desktop, nothing but text on disk.
    """
    from dax_quax.sources.pbip import open_pbip as _open

    return _open(path, attach_reports=attach_reports)


def open_file(path: str) -> Model:
    """Load a Model from a .pbix, .abf or PowerPivot .xlsx.

    The only source that carries metadata **and** storage metrics with no Power BI Desktop
    and no local cache. Needs the ``file`` extra.
    """
    from dax_quax.sources.file import load_file

    return load_file(path)


__all__ = [
    "Column",
    "Finding",
    "ScanScope",
    "Thresholds",
    "Verdict",
    "ColumnMetrics",
    "ConnectionDiscoveryError",
    "ContractViolation",
    "DaxQuaxError",
    "Measure",
    "MissingCapability",
    "Model",
    "ObjectRef",
    "Relationship",
    "Table",
    "__version__",
    "assess",
    "connect",
    "open_file",
    "open_pbip",
    "open_workspace",
]
