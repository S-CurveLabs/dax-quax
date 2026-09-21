"""Record a test fixture from a running Power BI Desktop instance.

    python tools/record_fixture.py contoso [--port 51247]

Writes tests/fixtures/<name>/rowsets.json in the shape conftest.py loads, and prints the
contract findings — which is the thing to read first, because it settles whether the
assumptions in sources/dmv.py match a real engine.

This is the only script in the repo that needs Power BI Desktop. The test suite never does.
"""

from __future__ import annotations

import argparse
import json
import pathlib

from dax_quax.sources import dmv
from dax_quax.sources.live import LiveConnection, discover_instances

FIXTURES = pathlib.Path(__file__).parents[1] / "tests" / "fixtures"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="fixture folder name, e.g. 'contoso'")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--keep-expressions",
        action="store_true",
        help="keep DAX and M expressions verbatim (they may contain real table/field names)",
    )
    args = parser.parse_args()

    port = args.port
    if port is None:
        instances = discover_instances()
        if not instances:
            print("no running Power BI Desktop instance found; open a .pbix first")
            return 1
        if len(instances) > 1:
            print(f"several instances running: {[i.port for i in instances]}; pass --port")
            return 1
        port = instances[0].port

    print(f"connecting to localhost:{port}")
    with LiveConnection(f"localhost:{port}") as conn:
        catalog = conn.catalog_name()
        rowsets = conn.rowsets()

    findings = dmv.verify_contract(rowsets)
    required = [f for f in findings if f.kind == "missing-required"]
    optional = [f for f in findings if f.kind == "missing-optional"]

    print(f"\ncatalog: {catalog}")
    for name, rows in rowsets.items():
        print(f"  {name:<18} {len(rows):>7} rows")

    print(f"\ncontract: {len(required)} required column(s) missing, {len(optional)} optional")
    for finding in required:
        print(f"  BROKEN  {finding.query}: {finding.detail}")
    for finding in optional:
        print(f"  note    {finding.query}: {finding.detail}")

    model = dmv.build_model(rowsets, name=catalog, source="live")
    print(f"\n{model!r}")
    if model.warnings:
        print("warnings:")
        for warning in model.warnings:
            print(f"  {warning}")

    if not args.keep_expressions:
        for row in rowsets.get("measures", []):
            if row.get("Expression"):
                row["Expression"] = "<redacted; rerun with --keep-expressions>"
        for row in rowsets.get("columns", []):
            if row.get("Expression"):
                row["Expression"] = "<redacted; rerun with --keep-expressions>"

    target = FIXTURES / args.name
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": f"RECORDED from {catalog} on localhost:{port}. Real DMV output.",
        **rowsets,
    }
    out = target / "rowsets.json"
    out.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 1 if required else 0


if __name__ == "__main__":
    raise SystemExit(main())
