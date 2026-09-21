"""Fetch real .pbix files to test the pbixray contract against.

    python tools/fetch_pbix.py
    pytest -m pbix

Like the .pbip corpus, these are borrowed rather than vendored: they are downloaded into
a gitignored path and CI never sees them (CONVENTIONS §10). `.pbix` is in .gitignore
already, so a stray copy cannot be committed by accident either.

WHY SEVERAL, AND WHY REAL
-------------------------
`sources/file.py` declares what pbixray returns as a set of frames and candidate column
names. Every one of those is a guess until a real file is read, and one file only proves
what that file happens to contain — a model with no calculated columns says nothing about
the dax_columns frame. So this fetches a spread: a DAX-heavy model, two large ones of
different shapes, and a small one for a quick pass.

**Every sample here is from microsoft/powerbi-desktop-samples, which is MIT.** That is a
deliberate limit rather than a coincidence: unlicensed samples were considered and left
out. Point DAXQUAX_TEST_PBIX at your own file to test against something this does not
carry.
"""

from __future__ import annotations

import pathlib
import sys
import urllib.parse
import urllib.request

DEST = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pbix"

MICROSOFT = "https://raw.githubusercontent.com/microsoft/powerbi-desktop-samples/main/"

#: local name -> (url, why this one)
SAMPLES: dict[str, tuple[str, str]] = {
    "revenue-opportunities.pbix": (
        MICROSOFT + "2026 Power BI Samples Revamp/Revenue Opportunities.pbix",
        "small and quick; the smoke test",
    ),
    "adventureworks-dw-2020.pbix": (
        MICROSOFT + "DAX/Adventure Works DW 2020.pbix",
        "the DAX reference model: many measures, calculated columns, hierarchies",
    ),
    "store-sales.pbix": (
        MICROSOFT + "2026 Power BI Samples Revamp/Store Sales.pbix",
        "the widest of the current samples",
    ),
    "employee-hiring.pbix": (
        MICROSOFT + "2026 Power BI Samples Revamp/Employee Hiring and History.pbix",
        "a second large one, different shape",
    ),
}


def fetch(*, force: bool = False) -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for name, (url, why) in SAMPLES.items():
        target = DEST / name
        if target.exists() and not force:
            print(f"  have {name}")
            continue
        print(f"  fetching {name}  ({why})")
        quoted = urllib.parse.quote(url, safe=":/")
        try:
            with urllib.request.urlopen(quoted) as response:  # noqa: S310 - fixed https
                target.write_bytes(response.read())
        except Exception as exc:  # noqa: BLE001 - one missing sample is not fatal
            print(f"    could not fetch: {exc}")
            continue
        print(f"    {target.stat().st_size / 1048576:.1f} MB")
    print(f"\nin {DEST}\nrun:  pytest -m pbix")
    return 0


if __name__ == "__main__":
    raise SystemExit(fetch(force="--force" in sys.argv))
