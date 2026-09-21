"""Live connection to a local Power BI Desktop instance.

The only Windows-only module in the package. Everything else must import and run on Linux,
because that is what CI does.

Verified end to end against Power BI Desktop 2.157.1354.0 on 2026-09-18, which corrected the
workspace roots below and settled the DMV contract in `dmv.py`.

THE ASSEMBLIES ARE NOT BUNDLED
------------------------------
ADOMD.NET is Microsoft's. Its EULA has a DISTRIBUTABLE CODE section, but the permission is
scoped to a REDIST list that the NuGet package does not contain, and one of its conditions
is requiring end users to accept terms protecting Microsoft — which a `pip install` does
not do. So the wheel ships none of it and `dax-quax install-runtime` fetches it per user.

A wheel cannot run code at install time, so first use is the only moment left to offer.
`_locate_runtime` does that: it asks if there is a terminal to ask at, obeys
DAXQUAX_AUTO_INSTALL_RUNTIME when set, and otherwise raises with the command to run.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any

from dax_quax.errors import ConnectionDiscoveryError, ContractViolation, DaxQuaxError
from dax_quax.model import Model
from dax_quax.sources import dmv

#: Where a running engine writes msmdsrv.port.txt. Verified against Power BI Desktop
#: 2.157.1354.0 (Microsoft Store build) on 2026-09-18.
#:
#: The Store build's is under USERPROFILE, not LOCALAPPDATA, and not under Packages\ at
#: all -- the first two entries were guesses from the redirected-storage layout and found
#: nothing on a machine where the engine was plainly running. Both are kept: the MSI build
#: and older Store builds are not ruled out by one observation, and a root that does not
#: exist costs a directory listing.
WORKSPACE_ROOTS = (
    r"{USERPROFILE}\Microsoft\Power BI Desktop Store App\AnalysisServicesWorkspaces",
    r"{LOCALAPPDATA}\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces",
    r"{LOCALAPPDATA}\Packages\Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe\LocalCache"
    r"\Local\Microsoft\Power BI Desktop Store App\AnalysisServicesWorkspaces",
)

_ADOMD_DLL = "Microsoft.AnalysisServices.AdomdClient.dll"
_runtime_ready = False


# -- discovery -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Instance:
    port: int
    workspace: pathlib.Path

    @property
    def data_source(self) -> str:
        return f"localhost:{self.port}"


def read_port_file(path: pathlib.Path) -> int:
    """Decode msmdsrv.port.txt.

    UTF-16, variously with a BOM, without one, and with trailing NUL padding. Trailing NULs
    are legitimate; interleaved ones mean we guessed the wrong width.
    """
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-16", "utf-16-le", "utf-8-sig", "utf-8", "latin-1"):
        try:
            candidate = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        candidate = candidate.strip("\x00﻿ \t\r\n")
        if "\x00" in candidate:
            continue
        text = candidate
        break
    match = re.search(r"\d{2,5}", text)
    if not match:
        raise ValueError(f"no port number in {path} (decoded {text!r})")
    return int(match.group())


def workspace_roots() -> list[pathlib.Path]:
    env = {
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
    }
    return [pathlib.Path(t.format(**env)) for t in WORKSPACE_ROOTS]


def discover_instances() -> list[Instance]:
    """Every local Power BI Desktop engine currently listening.

    One per open Desktop window, so more than one is normal.
    """
    found: list[Instance] = []
    for root in workspace_roots():
        if not root.is_dir():
            continue
        for port_file in root.glob("*/Data/msmdsrv.port.txt"):
            try:
                found.append(Instance(read_port_file(port_file), port_file.parents[1]))
            except (OSError, ValueError):
                continue
    return found


# -- CLR bootstrap ---------------------------------------------------------------------


def adomd_dir() -> pathlib.Path:
    """Where the vendored ADOMD.NET Core assemblies live.

    Order: DAXQUAX_ADOMD_DIR, the packaged _libs/ (vendored by tools/fetch_libs.py or by a
    wheel build), the per-user directory `dax-quax install-runtime` writes to, then the M0
    spike folder as a dev fallback.
    """
    from dax_quax.runtime import user_runtime_dir

    override = os.environ.get("DAXQUAX_ADOMD_DIR")
    candidates = [pathlib.Path(override)] if override else []
    candidates.append(pathlib.Path(__file__).parents[1] / "_libs")
    candidates.append(user_runtime_dir())
    candidates.append(
        pathlib.Path(__file__).parents[3]
        / "spikes/m0_adomd/libs"
        / "microsoft.analysisservices.adomdclient.netcore.retail.amd64/lib/netcoreapp3.0"
    )
    for candidate in candidates:
        if (candidate / _ADOMD_DLL).is_file():
            return candidate
    raise DaxQuaxError(
        f"{_ADOMD_DLL} not found, so a live connection is not possible yet.\n"
        "Run:  dax-quax install-runtime\n"
        "(a .pbip folder or a .pbix file needs none of this.)\n"
        "Looked in:\n"
        + "\n".join(f"  {c}" for c in candidates)
        + "\nOr set DAXQUAX_ADOMD_DIR to a folder that already has it."
    )


#: Asked at most once per process. A loop that re-prompts on every retry is worse than
#: the error it replaced.
_asked_to_install = False


def _ask_to_install() -> bool:
    """Offer to fetch the assemblies, if there is anybody to ask.

    A wheel cannot run code at install time, so `pip install dax-quax[live]` can never
    have fetched these. First use is the only moment left where a person is present and
    the answer is obviously useful.
    """
    global _asked_to_install
    from dax_quax.runtime import AUTO_INSTALL_ENV, auto_install_allowed

    allowed = auto_install_allowed()
    if allowed is False:
        return False
    if allowed is True:
        return True

    # Nobody stated a preference. Only ask if there is a terminal to ask at: prompting a
    # build server produces a hang that looks like a network stall.
    if _asked_to_install or not (sys.stdin and sys.stdin.isatty()):
        return False
    _asked_to_install = True
    print(
        "A live connection needs Microsoft's ADOMD.NET assemblies, which are not\n"
        "installed. They are about 5 MB from nuget.org, and they are not bundled\n"
        "here. Reading .pbip, .pbix, .abf and PowerPivot .xlsx files works without\n"
        f"them. Set {AUTO_INSTALL_ENV}=1\n"
        "to skip this question in future."
    )
    try:
        answer = input("Fetch them now? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _locate_runtime(*, need_amo: bool = False) -> pathlib.Path:
    """Where the assemblies are, fetching them first if that is allowed.

    `adomd_dir` only looks. This is the one place that may also act, so the lookup stays
    a pure question that tests can ask without a network.
    """
    from dax_quax.runtime import install

    try:
        found = adomd_dir()
    except DaxQuaxError:
        if not _ask_to_install():
            raise
        where, names = install()
        print(f"installed {len(names)} assemblies into {where}")
        return adomd_dir()

    # ADOMD present but AMO absent: a checkout that ran fetch_libs.py without --with-tom,
    # which is fine for everything except tracing.
    amo_absent = need_amo and not all((found / name).is_file() for name in _AMO_DLLS)
    if amo_absent and _ask_to_install():
        where, names = install()
        print(f"installed {len(names)} assemblies into {where}")
        return adomd_dir()
    return found


def _runtime_config(libs: pathlib.Path) -> pathlib.Path:
    """The .runtimeconfig.json coreclr boots from, written if it is not already there.

    Written under the per-user runtime directory rather than TEMP. This file decides which
    .NET runtime starts and where it probes for assemblies, and on Windows TEMP is shared
    between users — a predictable name anyone can pre-create is not a name to boot a
    runtime from.
    """
    from dax_quax.runtime import user_runtime_dir

    beside = libs.parent / "daxquax.runtimeconfig.json"
    if beside.is_file():
        return beside

    config = user_runtime_dir() / "daxquax.runtimeconfig.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "tfm": "netcoreapp3.0",
                    "framework": {"name": "Microsoft.NETCore.App", "version": "10.0.0"},
                    "rollForward": "latestMajor",
                }
            }
        ),
        encoding="utf-8",
    )
    return config


def ensure_runtime() -> None:
    """Boot coreclr and load the client assemblies. Idempotent."""
    global _runtime_ready
    if _runtime_ready:
        return

    libs = _locate_runtime()
    config = _runtime_config(libs)

    from clr_loader import get_coreclr
    from pythonnet import set_runtime

    set_runtime(get_coreclr(runtime_config=str(config)))
    import clr  # noqa: F401
    from System.Reflection import Assembly

    Assembly.LoadFrom(str(libs / _ADOMD_DLL))
    _runtime_ready = True


#: AMO/TOM, needed only for tracing. Loaded by path for the same reason ADOMD is: these
#: assemblies are vendored into _libs/ and are not on any probing path, so `clr.AddReference`
#: cannot find them and `from Microsoft.AnalysisServices import Server` fails with an
#: ImportError that reads like the type is missing rather than the assembly unloaded.
_AMO_DLLS = (
    "Microsoft.AnalysisServices.Runtime.Core.dll",
    "Microsoft.AnalysisServices.Runtime.Windows.dll",
    "Microsoft.AnalysisServices.Core.dll",
    "Microsoft.AnalysisServices.Tabular.dll",
    "Microsoft.AnalysisServices.dll",
)
_amo_ready = False


def ensure_amo() -> None:
    """Load the AMO assemblies. Idempotent, and raises with what to run if they are absent.

    They ship in a second NuGet package, so a checkout that ran `fetch_libs.py` without
    --with-tom has ADOMD and no AMO. That is a fine state for everything but `bench`.
    """
    global _amo_ready
    if _amo_ready:
        return
    ensure_runtime()

    libs = _locate_runtime(need_amo=True)
    missing = [name for name in _AMO_DLLS if not (libs / name).is_file()]
    if missing:
        raise DaxQuaxError(
            "tracing needs the AMO assemblies, which are not installed "
            f"({missing[0]} is absent). Run:  dax-quax install-runtime"
        )

    from System.Reflection import Assembly

    for name in _AMO_DLLS:
        Assembly.LoadFrom(str(libs / name))
    _amo_ready = True


# -- connection ------------------------------------------------------------------------


class LiveConnection:
    """Thin wrapper over AdomdConnection. Use as a context manager."""

    def __init__(self, data_source: str, database: str | None = None, timeout: int = 15) -> None:
        self.data_source = data_source
        self.database = database
        self.timeout = timeout
        self._conn: Any = None

    def __enter__(self) -> LiveConnection:
        ensure_runtime()
        from Microsoft.AnalysisServices.AdomdClient import AdomdConnection

        parts = [f"Data Source={self.data_source}", f"Connect Timeout={self.timeout}"]
        if self.database:
            parts.append(f"Catalog={self.database}")
        self._conn = AdomdConnection(";".join(parts))
        self._conn.Open()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            self._conn.Close()
            self._conn = None

    def catalog_name(self) -> str:
        databases = self.execute("SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS")
        if not databases:
            raise ContractViolation(f"no catalog on {self.data_source}")
        return str(databases[0]["CATALOG_NAME"])

    def session_spid(self) -> int | None:
        """The engine's SPID for this connection, or None if it cannot be resolved.

        ADOMD knows its own SessionID (a GUID); DISCOVER_SESSIONS maps that to the SPID
        that trace events carry. Without this a trace cannot tell this connection's
        queries from the ones Power BI Desktop fires as you click around.
        """
        if self._conn is None:
            return None
        session_id = str(getattr(self._conn, "SessionID", "") or "")
        if not session_id:
            return None
        rows = self.execute("SELECT SESSION_ID, SESSION_SPID FROM $SYSTEM.DISCOVER_SESSIONS")
        for row in rows:
            if str(row.get("SESSION_ID")) == session_id:
                spid = row.get("SESSION_SPID")
                return int(spid) if spid is not None else None
        return None

    def execute_non_query(self, statement: str) -> None:
        """Run a statement that returns no rowset, such as an XMLA ClearCache.

        ExecuteReader on one of these raises AdomdUnknownResponseException ("the result
        set returned by the server is not a rowset"), which is how every cold benchmark
        died before it timed anything.
        """
        if self._conn is None:
            raise DaxQuaxError("connection is not open")
        from Microsoft.AnalysisServices.AdomdClient import AdomdCommand

        AdomdCommand(statement, self._conn).ExecuteNonQuery()

    def execute(self, statement: str) -> list[dict[str, Any]]:
        """Run one statement and materialise the rowset as a list of dicts."""
        if self._conn is None:
            raise DaxQuaxError("connection is not open")
        from Microsoft.AnalysisServices.AdomdClient import AdomdCommand

        command = AdomdCommand(statement, self._conn)
        reader = command.ExecuteReader()
        try:
            names = [str(reader.GetName(i)) for i in range(reader.FieldCount)]
            rows: list[dict[str, Any]] = []
            while reader.Read():
                rows.append(
                    {name: _py(reader.GetValue(i)) for i, name in enumerate(names)}
                )
            return rows
        finally:
            reader.Close()

    def rowsets(self, names: tuple[str, ...] = ()) -> dmv.Rowsets:
        """Run the declared DMV queries and return their rowsets.

        A query marked ``required=False`` that fails is recorded as empty rather than
        raising — not every model has storage rows (DirectQuery) or partitions.
        """
        wanted = names or tuple(dmv.QUERIES)
        out: dmv.Rowsets = {}
        for name in wanted:
            query = dmv.QUERIES[name]
            try:
                out[name] = self.execute(query.statement)
            except Exception as exc:  # noqa: BLE001 - .NET exceptions are not Python ones
                if query.required:
                    raise ContractViolation(f"{name}: {query.statement} failed: {exc}") from exc
                out[name] = []
        return out


def _py(value: Any) -> Any:
    """Convert a .NET value to a plain Python one. DBNull becomes None."""
    if value is None:
        return None
    type_name = type(value).__name__
    if type_name == "DBNull":
        return None
    if type_name in {"String", "str"}:
        return str(value)
    if type_name in {"Int64", "Int32", "UInt64", "UInt32", "Int16", "Byte"}:
        return int(value)
    if type_name in {"Double", "Single", "float"}:
        return float(value)
    if type_name == "Boolean":
        return bool(value)
    # System.Decimal is not a Python number and float() refuses it outright. The DMVs
    # never return one, so this only surfaced the first time a real DAX query ran: a
    # currency column came back and the whole benchmark died converting the result.
    if type_name == "Decimal":
        return float(str(value))
    if type_name == "DateTime":
        return str(value)
    return value


# -- entry point -----------------------------------------------------------------------


def connect(port: int | None = None, database: str | None = None) -> Model:
    """Load a Model from a running local Power BI Desktop instance."""
    if port is None:
        instances = discover_instances()
        if not instances:
            raise ConnectionDiscoveryError(
                "no running Power BI Desktop instance found. Open a .pbix, or pass port=.\n"
                "Looked in:\n" + "\n".join(f"  {r}" for r in workspace_roots())
            )
        if len(instances) > 1:
            listed = ", ".join(str(i.port) for i in instances)
            raise ConnectionDiscoveryError(
                f"{len(instances)} Power BI Desktop instances are running (ports {listed}). "
                "Pass port= to choose one."
            )
        port = instances[0].port

    with LiveConnection(f"localhost:{port}", database) as conn:
        name = database or conn.catalog_name()
        rowsets = conn.rowsets()

    return dmv.build_model(rowsets, name=name, source="live")


__all__ = [
    "Instance",
    "LiveConnection",
    "adomd_dir",
    "connect",
    "discover_instances",
    "ensure_runtime",
    "read_port_file",
    "workspace_roots",
]
