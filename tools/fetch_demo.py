"""Fetch a real .pbip corpus to test against.

CONVENTIONS §10 promises the suite runs with no Power BI Desktop, no .NET and no network,
so this corpus is never committed and never fetched by CI. It is cloned on demand, into a
gitignored path, and the tests that use it are marked `demo` and deselected by default.

    python tools/fetch_demo.py
    pytest -m demo

The corpus is https://github.com/RuiRomano/pbip-demo — three semantic models and five
reports, exported by a real Power BI Desktop. It carries no licence file, which is the
reason it is borrowed rather than vendored.

WHY THIS EXISTS
---------------
Six of this project's contracts were declared against documentation rather than output
(§10). The first time this corpus met the loaders it found four of them wrong. Synthetic
fixtures cannot do that: they encode the same assumptions the parser does, so they agree
with it by construction.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

REPO = "https://github.com/RuiRomano/pbip-demo.git"
DEST = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "demo"


def fetch(*, force: bool = False) -> pathlib.Path:
    if DEST.exists():
        if not force:
            print(f"already present: {DEST}")
            return DEST
        shutil.rmtree(DEST)
    DEST.parent.mkdir(parents=True, exist_ok=True)
    print(f"cloning {REPO}")
    subprocess.run(
        ["git", "clone", "--depth", "1", REPO, str(DEST)],
        check=True,
        capture_output=True,
        text=True,
    )
    # The clone carries scripts (deploy.py, bpa.ps1) that nothing here runs. Only the
    # .pbip artefacts are of interest, and they are read as data.
    print(f"fetched to {DEST}")
    print("run:  pytest -m demo")
    return DEST


if __name__ == "__main__":
    raise SystemExit(0 if fetch(force="--force" in sys.argv) else 1)
