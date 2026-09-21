"""Exception hierarchy. Everything raised by this library derives from DaxQuaxError."""

from __future__ import annotations


class DaxQuaxError(Exception):
    """Base for every error this library raises."""


class ConnectionDiscoveryError(DaxQuaxError):
    """No local Analysis Services instance could be found, or too many were."""


class ConnectionError_(DaxQuaxError):
    """A connection was found but could not be opened."""


class MissingCapability(DaxQuaxError):
    """An analysis needs something the loaded model's source cannot supply.

    The message names both what is absent and which source would provide it, because
    "metrics unavailable" without that second half sends people hunting in the wrong place.
    """

    HINTS = {
        "metrics": "connect to a running instance, open a .pbix/.abf, "
        "or use a .pbip folder that still has its .pbi/cache.abf",
        "report": "load a .pbip folder — the live engine has no report layer",
        "expressions": "any source supplies these; the model may have failed to load",
        "dependencies": "only a live connection exposes DISCOVER_CALC_DEPENDENCY",
        "execute": "only a live connection can run a query",
    }

    def __init__(self, needed: frozenset[str] | set[str], have: frozenset[str]) -> None:
        missing = sorted(set(needed) - set(have))
        lines = [f"model is missing capability: {', '.join(missing)}"]
        fallback = "no source in this build provides it"
        lines += [f"  {m}: {self.HINTS.get(m, fallback)}" for m in missing]
        lines.append(f"  loaded model has: {', '.join(sorted(have)) or '(none)'}")
        super().__init__("\n".join(lines))
        self.missing = tuple(missing)


class ContractViolation(DaxQuaxError):
    """A DMV returned a shape we do not recognise. See sources/dmv.py."""
