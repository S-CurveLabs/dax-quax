"""Vendor the ADOMD.NET Core assemblies into src/dax_quax/_libs/.

Not committed — the DLLs are ~1 MB and come straight from nuget.org. Run once per checkout,
or as a build step when producing a wheel.

    python tools/fetch_libs.py [--with-tom] [--version 19.84.1]

Localisation resources are skipped: they are 18 MB of the 24 MB package and English is
enough for a library that only reads DMV rowsets.
"""

from __future__ import annotations

import argparse
import io
import pathlib
import shutil
import urllib.request
import zipfile

ADOMD = "microsoft.analysisservices.adomdclient.netcore.retail.amd64"
TOM = "microsoft.analysisservices.netcore.retail.amd64"
DEFAULT_VERSION = "19.84.1"
TFM = "lib/netcoreapp3.0/"
DEST = pathlib.Path(__file__).parents[1] / "src" / "dax_quax" / "_libs"


def fetch(package: str, version: str, dest: pathlib.Path) -> list[str]:
    url = f"https://api.nuget.org/v3-flatcontainer/{package}/{version}/{package}.{version}.nupkg"
    print(f"  fetching {package} {version}")
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed host
        payload = response.read()

    taken: list[str] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for entry in archive.namelist():
            if not entry.startswith(TFM) or not entry.endswith(".dll"):
                continue
            relative = entry[len(TFM) :]
            if "/" in relative:  # a locale subfolder — skip
                continue
            target = dest / relative
            target.write_bytes(archive.read(entry))
            taken.append(relative)
    return taken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument(
        "--with-tom",
        action="store_true",
        help="also vendor AMO/TOM (+4.5 MB); only needed if a loader uses the object model",
    )
    parser.add_argument("--clean", action="store_true", help="empty the destination first")
    args = parser.parse_args()

    if args.clean and DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True, exist_ok=True)

    print(f"vendoring into {DEST}")
    files = fetch(ADOMD, args.version, DEST)
    if args.with_tom:
        files += fetch(TOM, args.version, DEST)

    total = sum((DEST / f).stat().st_size for f in set(files))
    print(f"\n{len(set(files))} assemblies, {total / 1024 / 1024:.2f} MB")
    for name in sorted(set(files)):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
