# dax-quax

**For every object in a Power BI model: what does it cost, what depends on it, and can it be deleted?**

Not a DAX Studio clone. It answers one question, and it is careful about the answer, because
telling you to delete a column that something still uses is the only way a tool like this can
really hurt you.

```
Contoso  (pbip)
  87 objects, 214.3 MB measured
  REMOVE    6      41.2 MB   (19.2% of measured)
  REVIEW    3       8.1 MB
  UNKNOWN   0
  KEEP     78

top 3 by size:
  REMOVE     28.4 MB  Sales[Order Number]
             Remove it from the Power Query load.
             Contoso.SemanticModel/definition/tables/Sales.tmdl:3

scope: 3 reports scanned
```

## Install

Not on PyPI yet, so install from the repo:

```bash
pip install "dax-quax[file,report] @ git+https://github.com/S-CurveLabs/dax-quax.git"
```

Reading a `.pbip` project folder or a `.pbix` file needs nothing else — no Power BI Desktop,
no .NET, and it works on Linux and macOS. To scan a *running* Power BI Desktop you also need
the Microsoft client assemblies, which cannot ship in the wheel:

```bash
pip install "dax-quax[live] @ git+https://github.com/S-CurveLabs/dax-quax.git"
dax-quax install-runtime
```

That fetches ADOMD.NET from nuget.org into a per-user folder. Windows only, because the
engine it talks to is.

If you skip it, the first live connection offers to do it for you when there is a terminal
to ask at. Set `DAXQUAX_AUTO_INSTALL_RUNTIME=1` to answer yes in advance, or `=0` to be left
alone — unattended runs are never prompted, because a question nobody can answer is a hang.

They are not in the wheel on purpose. The assemblies are Microsoft's: the EULA does have a
distributable-code clause, but it is scoped to a REDIST list the NuGet package does not
contain, and one of its conditions is requiring end users to accept terms protecting
Microsoft, which `pip install` does not do.

| Extra | For |
|---|---|
| `file` | `.pbix`, `.abf`, PowerPivot `.xlsx` — metadata **and** sizes with nothing installed |
| `report` | the HTML report |
| `serve` | `serve`, the browsable version |
| `live` | a running Power BI Desktop (plus `dax-quax install-runtime`) |
| `mcp` | the MCP server |

## Use it

```bash
dax-quax scan --pbip  path/to/Model.SemanticModel --report-folder path/to/Name.Report
dax-quax scan --pbix  path/to/file.pbix
dax-quax scan                                  # a running Power BI Desktop
dax-quax scan --workspace path/to/folder       # every model, against every report over it

dax-quax report --pbip DIR --out report.html   # one self-contained file, no network
dax-quax serve  --pbip DIR                     # the same, plus lineage and a rescan
dax-quax bench  "EVALUATE ..." --cold          # storage-engine vs formula-engine timings
dax-quax mcp    --pbip DIR                     # answer questions about the model, read-only
```

As a library:

```python
from dax_quax import open_pbip

model = open_pbip("path/to/Model.SemanticModel")
model.findings()          # ranked cost x usage
model.lineage()           # what breaks if this goes
model.where("[Margin]")   # the file and line that define it
```

## What makes a verdict trustworthy

A verdict is only as good as what was scanned, so **the scope travels with it** instead of
living in a header you might not have read.

- `REMOVE` means *nothing in what was scanned referenced it*. It is a question to put to a
  person, not an instruction.
- **If no report was scanned, nothing is ever `REMOVE`.** Every unreferenced object is
  `UNKNOWN`, because a report you left out is usage that was not counted.
- In a workspace scan, **one report that cannot be matched to its model degrades every
  verdict in the whole directory.**
- Anything the scan could not see is listed on the report, next to the verdicts.

A false `REMOVE` is the expensive failure. `UNKNOWN` is free. The tool is built to prefer the
second, and several places say so out loud rather than quietly rounding a gap to zero — a size
it could not measure stays `None` and never becomes `0 B`.

## For agents

`dax-quax mcp` exposes the model over MCP, read-only, so you can ask an assistant what is safe
to delete and why. The surface is fenced three ways: every tool is a function of a snapshot
that holds no connection, an allowlist controls what is reachable, and each tool is advertised
with the protocol's own `readOnlyHint`. Every payload carrying a verdict also carries the scope
caveat — an agent gets one payload and no headline, so the caveat is attached centrally rather
than left to each tool to remember.

```jsonc
{"dax-quax": {"command": "dax-quax", "args": ["mcp", "--pbip", "C:/path/Model.SemanticModel"]}}
```

## Limits worth knowing before you trust it

- Consumers outside the artefacts you scan — Excel, paginated reports, anything on the XMLA
  endpoint — are invisible. A workspace scan narrows this; it cannot close it.
- Perspectives and translations are not walked. Neither can cause a false `REMOVE`.
- `.pbix` sizes come from [pbixray](https://github.com/Hugoberry/pbixray), which reports no
  encoding, no row count and no hierarchy size — those stay absent rather than becoming zero.
- DAX references are *extracted*, not parsed. Dynamic references built with string
  manipulation cannot be seen by anything that does not run the query.
- Version 0.1. Six of the seven declared interfaces to Microsoft's formats and APIs turned out
  to be wrong the first time they met a real engine; they are fixed and pinned by tests now,
  but that is the nature of the terrain.

## Development

```bash
python -m venv .venv && .venv/Scripts/activate   # source .venv/bin/activate elsewhere
pip install -e ".[dev,file,report,serve,mcp]"
pytest -q                      # no Power BI, no .NET, no network
python tools/check_imports.py  # nothing may need an extra to import
```

Run `pytest` from inside the environment you installed into. The extras carry the test
dependencies, so a `pytest` picked up from somewhere else fails during collection on an
import — `No module named 'networkx'` — rather than on anything you changed.

Tests that need something real are deselected by default — `-m live` for a running instance,
`-m pbix` after `python tools/fetch_pbix.py`, `-m demo` after `python tools/fetch_demo.py`.

## License

MIT.
