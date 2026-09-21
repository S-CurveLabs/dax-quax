"""Fetch the ADOMD.NET Core assemblies a live connection needs.

`pip install dax-quax` cannot carry these: they are Microsoft's, redistributing them is a
question nobody here has answered, and they are 5 MB nobody needs unless they are scanning
a running Power BI Desktop. So they are fetched on request:

    dax-quax install-runtime

Only `sources/live.py` needs them. Reading a .pbip folder or a .pbix file needs nothing
installed at all, which is the point of having three sources.

WHERE THEY GO
-------------
Into a per-user directory, not into site-packages. Writing inside an installed package
works in a virtualenv and fails on a system install, and the failure arrives as a
PermissionError in the middle of what looked like a download. `tools/fetch_libs.py` still
vendors into the package for building a wheel; `sources/live.adomd_dir` checks both.

Stdlib only, on purpose: this has to run before anything optional is installed.
"""

from __future__ import annotations

import io
import os
import pathlib
import urllib.request
import zipfile

__all__ = [
    "ADOMD_DLL",
    "AUTO_INSTALL_ENV",
    "DEFAULT_VERSION",
    "auto_install_allowed",
    "install",
    "user_runtime_dir",
]

#: The NuGet packages. The netcore builds, because pythonnet here boots coreclr.
ADOMD_PACKAGE = "microsoft.analysisservices.adomdclient.netcore.retail.amd64"
TOM_PACKAGE = "microsoft.analysisservices.netcore.retail.amd64"
DEFAULT_VERSION = "19.84.1"

#: Assemblies live under this path inside the .nupkg; locale subfolders are 18 of its
#: 24 MB and English is enough for reading DMV rowsets.
_TFM = "lib/netcoreapp3.0/"

ADOMD_DLL = "Microsoft.AnalysisServices.AdomdClient.dll"

_FEED = "https://api.nuget.org/v3-flatcontainer/{package}/{version}/{package}.{version}.nupkg"


def user_runtime_dir() -> pathlib.Path:
    """Where `install-runtime` puts the assemblies for this user."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = pathlib.Path(base) if base else pathlib.Path.home() / ".local" / "share"
    return root / "dax-quax" / "runtime"


def fetch_package(package: str, version: str, dest: pathlib.Path) -> list[str]:
    """Download one NuGet package and unpack its assemblies into ``dest``."""
    url = _FEED.format(package=package, version=version)
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed host
        payload = response.read()

    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    taken: list[str] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for entry in archive.namelist():
            if not entry.startswith(_TFM) or not entry.endswith(".dll"):
                continue
            relative = entry[len(_TFM) :]
            # Zip entries always separate with "/", so a backslash means a hand-built
            # entry: on Windows `..\..\evil.dll` is a path out of dest, and on Linux it
            # is one odd filename that would still be written and reported as taken.
            if "/" in relative or "\\" in relative:  # a locale subfolder, or hostile
                continue
            # A zip entry is not a filename until it has been checked; resolving and
            # comparing stays as the backstop for anything the test above misses.
            target = (dest / relative).resolve()
            if target.parent != dest:
                continue
            target.write_bytes(archive.read(entry))
            taken.append(relative)
    return taken


def install(
    *,
    dest: pathlib.Path | None = None,
    version: str = DEFAULT_VERSION,
    with_tom: bool = True,
) -> tuple[pathlib.Path, list[str]]:
    """Fetch the assemblies and return where they landed and what arrived.

    ``with_tom`` defaults to True because `bench` needs AMO for tracing, and a user who
    installs the runtime at all is far more likely to want both than to be counting the
    4.5 MB difference.
    """
    target = dest or user_runtime_dir()
    names = fetch_package(ADOMD_PACKAGE, version, target)
    if with_tom:
        names += fetch_package(TOM_PACKAGE, version, target)
    return target, sorted(set(names))


#: Set this to 1 to let a live connection fetch the assemblies without asking, or to 0 to
#: forbid it. Unset means "ask, if there is somebody to ask".
AUTO_INSTALL_ENV = "DAXQUAX_AUTO_INSTALL_RUNTIME"

_YES = frozenset({"1", "true", "yes", "on"})
_NO = frozenset({"0", "false", "no", "off"})


def auto_install_allowed() -> bool | None:
    """True, False, or None for "not stated".

    Three states rather than two on purpose. An unset variable is not the same answer as
    an explicit 0: the first means ask a person if one is there, the second means do not,
    and collapsing them would either nag a script or silently download on a build server.
    """
    raw = os.environ.get(AUTO_INSTALL_ENV)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _YES:
        return True
    if value in _NO:
        return False
    return None
