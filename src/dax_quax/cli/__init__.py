"""Six verbs: scan, report, serve, bench, mcp, install-runtime.

A thin wrapper over the library. Nothing is implemented here that is not available as a
function, because the library is the product and the CLI is one of its consumers.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from dax_quax.analysis.usage import Thresholds, assess, findings_frame, scope_of, summary
from dax_quax.errors import DaxQuaxError
from dax_quax.model import Model

__all__ = ["main"]

_MB = 1024 * 1024


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_argument_group("model source")
    source.add_argument("--port", type=int, help="port of a running Power BI Desktop instance")
    source.add_argument(
        "--pbip",
        type=pathlib.Path,
        help="a .pbip project folder. Brings its own report layer, but has no storage "
        "metrics unless the project still has its local .pbi/cache.abf",
    )
    source.add_argument(
        "--workspace",
        type=pathlib.Path,
        metavar="DIR",
        help="a directory of .pbip artefacts. Assesses every model against every report "
        "over it, which is the only way a column one report ignores is not reported as "
        "removable when another report depends on it",
    )
    source.add_argument(
        "--pbix",
        type=pathlib.Path,
        metavar="FILE",
        help="a .pbix, .abf or PowerPivot .xlsx. The only source with metadata and sizes "
        "together and no Power BI Desktop, but it has no report layer",
    )
    source.add_argument(
        "--rowsets",
        type=pathlib.Path,
        help="replay a recorded scan (a rowsets.json from tools/record_fixture.py) "
        "instead of connecting",
    )
    source.add_argument(
        "--report-folder",
        type=pathlib.Path,
        action="append",
        default=[],
        metavar="PATH",
        help="a .pbip or .Report folder; repeat for every report over this model. "
        "Without any, unused objects are UNKNOWN rather than REMOVE. Passing only some "
        "of them is how a live object gets deleted",
    )
    parser.add_argument(
        "--large-mb",
        type=float,
        default=10.0,
        help="size at which a rule starts questioning an object (default: 10)",
    )


def _load(args: argparse.Namespace) -> Model:
    if args.pbix:
        from dax_quax.sources.file import load_file

        model = load_file(args.pbix)
    elif args.pbip:
        from dax_quax.sources.pbip import open_pbip

        model = open_pbip(args.pbip)
    elif args.rowsets:
        from dax_quax.sources import dmv

        payload = json.loads(args.rowsets.read_text(encoding="utf-8"))
        rowsets = {k: v for k, v in payload.items() if not k.startswith("_")}
        model = dmv.build_model(rowsets, name=args.rowsets.parent.name, source="replay")
    else:
        from dax_quax import connect

        model = connect(port=args.port)

    if args.report_folder:
        from dax_quax.report import attach_report, load_report

        attach_report(model, *(load_report(folder) for folder in args.report_folder))
    return model


def _size(value: int | None) -> str:
    """ASCII-only byte formatting.

    render/ uses an em-dash for unmeasured values, which a Windows console in a legacy
    code page shows as a replacement character. Terminal output stays ASCII.
    """
    from dax_quax.render.report import format_bytes

    return "n/a" if value is None else format_bytes(value)


def _thresholds(args: argparse.Namespace) -> Thresholds:
    return Thresholds(large_bytes=int(args.large_mb * _MB))


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.workspace:
        return _scan_workspace(args)
    model = _load(args)
    findings = assess(model, thresholds=_thresholds(args))
    head = summary(findings)

    if args.json:
        payload: dict[str, Any] = {
            "model": model.name,
            "source": model.source,
            "summary": head,
            "findings": findings_frame(findings).to_dict(orient="records"),
            "warnings": model.warnings,
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    # "0 B / 0.0%" is a lie when the source could not measure anything at all, so the
    # size columns are dropped entirely rather than filled with zeros.
    sized = model.has("metrics")

    print(f"{model.name}  ({model.source})")
    for warning in model.warnings:
        # A warning the JSON carried and the terminal did not. Every one of these is a
        # reason a verdict below may be wrong, so they go above the verdicts.
        print(f"  ! {warning}")
    if sized:
        print(f"  {head['objects']} objects, {_size(head['measured_bytes'])} measured")
        print(
            f"  REMOVE {head['removable_objects']:>4}  {_size(head['removable_bytes']):>10}"
            f"   ({head['removable_pct']:.1f}% of measured)"
        )
        print(f"  REVIEW {head['review_objects']:>4}  {_size(head['review_bytes']):>10}")
    else:
        print(f"  {head['objects']} objects, no storage metrics from this source")
        print(f"  REMOVE {head['removable_objects']:>4}")
        print(f"  REVIEW {head['review_objects']:>4}")
    print(f"  UNKNOWN{head['unknown_objects']:>4}")
    print(f"  KEEP   {head['keep_objects']:>4}")

    actionable = [f for f in findings if f.is_actionable][: args.top]
    if actionable:
        print(f"\ntop {len(actionable)}" + (" by size:" if sized else ":"))
        for finding in actionable:
            size = f"{_size(finding.bytes):>10}  " if sized else ""
            print(f"  {finding.verdict:<8} {size}{finding.key}")
            if finding.action:
                print(f"           {finding.action}")
            where = model.where(finding.key)
            if where:
                # Plain path:line, which most terminals turn into a link themselves.
                print(f"           {where.describe(model.root)}")

    _print_report_findings(model, args)

    scope = scope_of(model)
    print(f"\nscope: {scope.describe()}")
    if not scope.trustworthy:
        print(
            "unreferenced objects are UNKNOWN rather than REMOVE. Pass --report-folder once\n"
            "per report over this model; a report you leave out is usage that is not counted."
        )
    return _baseline_step(args, model, findings)


def _print_report_findings(model: Model, args: argparse.Namespace) -> None:
    """What is wrong with the reports, as opposed to with the model."""
    from dax_quax.analysis.report_findings import ALREADY_BROKEN, report_findings, sort_key

    findings = sorted(report_findings(model), key=sort_key)
    if not findings:
        return

    broken = [f for f in findings if f.kind in ALREADY_BROKEN]
    headline = f"\nreport layer: {len(findings)} finding(s)"
    if broken:
        headline += f", {len(broken)} already broken"
    print(headline)
    for finding in findings[: args.top]:
        print(f"  {finding.describe()}")
        if finding.file:
            print(f"  {'':<18} {_relative(finding.file, model.root)}")
    if len(findings) > args.top:
        print(f"  ... and {len(findings) - args.top} more")


def _relative(path: pathlib.Path, root: pathlib.Path | None) -> str:
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _baseline_step(args: argparse.Namespace, model: Model, findings: list) -> int:
    """Save this scan, compare it against a saved one, and apply any CI gates."""
    from dax_quax.analysis.baseline import Baseline, Gates, diff

    if not (getattr(args, "save", None) or getattr(args, "compare", None)):
        return _gate_without_baseline(args)

    current = Baseline.from_findings(model, findings)

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(current.to_dict(), indent=2), encoding="utf-8")
        print(f"\nsaved baseline to {args.save}")

    if not args.compare:
        return _gate_without_baseline(args)

    if not args.compare.is_file():
        print(f"error: no baseline at {args.compare}", file=sys.stderr)
        return 1

    previous = Baseline.from_dict(json.loads(args.compare.read_text(encoding="utf-8")))
    delta = diff(previous, current)

    print(f"\nsince {args.compare} ({previous.taken_at}): {delta.describe()}")
    for change in (*delta.added, *delta.gone, *delta.verdict_changes, *delta.size_changes)[
        : args.top
    ]:
        print(f"  {change.describe()}")
    for note in delta.notes:
        print(f"  ! {note}")

    gates = Gates(
        fail_on_new_remove=args.fail_on_new_remove,
        max_growth_bytes=(
            int(args.max_growth_mb * _MB) if args.max_growth_mb is not None else None
        ),
        fail_on_scope_loss=args.fail_on_scope_loss,
    )
    failures = delta.failures(gates)
    for reason in failures:
        print(f"FAIL: {reason}", file=sys.stderr)
    return 1 if failures else 0


def _gate_without_baseline(args: argparse.Namespace) -> int:
    """A gate with nothing to compare against has not passed; it has not run."""
    from dax_quax.analysis.baseline import Gates

    gates = Gates(
        fail_on_new_remove=getattr(args, "fail_on_new_remove", False),
        max_growth_bytes=(
            int(args.max_growth_mb * _MB)
            if getattr(args, "max_growth_mb", None) is not None
            else None
        ),
        fail_on_scope_loss=getattr(args, "fail_on_scope_loss", False),
    )
    if gates.any:
        print(
            "FAIL: a CI gate was set but no --compare baseline was given, so nothing was "
            "checked",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from dax_quax.render.report import render_report

    if args.workspace:
        from dax_quax.render.workspace import render_workspace
        from dax_quax.sources.workspace import discover_workspace

        workspace = discover_workspace(args.workspace)
        out = args.out
        if out.suffix:  # a filename was given where a directory is needed
            out = out.parent / out.stem
        index = render_workspace(
            workspace, out, thresholds=_thresholds(args), editor=args.editor
        )
        pages = len(workspace.models) + 1
        print(f"wrote {index} and {pages - 1} model page(s)")
        print(f"  {workspace.describe()}")
        return 0

    model = _load(args)
    path = render_report(model, args.out, thresholds=_thresholds(args), editor=args.editor)
    print(f"wrote {path}  ({path.stat().st_size / 1024:.0f} KB)")
    return 0


def _workspace_payload(workspace: Any, args: argparse.Namespace) -> dict[str, Any]:
    """A workspace scan as JSON, for a pipeline.

    Carries the coverage and the unmatched reports, not just the findings: a consumer that
    cannot see how complete the scan was cannot tell a REMOVE from a guess.
    """
    return {
        "root": str(workspace.root),
        "coverage": workspace.describe(),
        "trustworthy": not workspace.unmatched,
        "reports": [
            {
                "name": link.name,
                "model": link.model,
                "by_path": link.by_path,
                "by_connection": link.by_connection,
                "reason": link.reason,
            }
            for link in workspace.links
        ],
        "unmatched": [link.name for link in workspace.unmatched],
        "orphan_models": workspace.orphan_models(),
        "cross_model": [
            {"reader": reader, "target": target or None}
            for reader, target in workspace.cross_model
        ],
        "models": {
            name: {
                "scope": workspace.scope_for(name).describe(),
                "summary": summary(workspace.findings_for(name, thresholds=_thresholds(args))),
                "findings": findings_frame(
                    workspace.findings_for(name, thresholds=_thresholds(args))
                ).to_dict(orient="records"),
            }
            for name in sorted(workspace.models)
        },
        "warnings": workspace.warnings,
    }


def _scan_workspace(args: argparse.Namespace) -> int:
    """Assess every model in a directory against every report over it."""
    from dax_quax.analysis.usage import summary
    from dax_quax.sources.workspace import discover_workspace

    workspace = discover_workspace(args.workspace)

    if args.json:
        print(json.dumps(_workspace_payload(workspace, args), indent=2, default=str))
        return 0

    print(f"{workspace.root}")
    print(f"  {workspace.describe()}")

    for link in workspace.links:
        target = link.model or "UNMATCHED"
        print(f"    {link.name:<24} -> {target}")
        if link.reason:
            print(f"        {link.reason}")

    orphans = workspace.orphan_models()
    if orphans:
        listed = ", ".join(orphans)
        print(f"\n  orphan model(s) - nothing in this scan references them: {listed}")
    for reader, target in workspace.cross_model:
        print(f"  {reader} reads {target or 'another semantic model (target unknown)'}")

    for name in sorted(workspace.models):
        findings = workspace.findings_for(name, thresholds=_thresholds(args))
        head = summary(findings)
        scope = workspace.scope_for(name)
        print(f"\n{name}  ({scope.describe()})")
        print(
            f"  REMOVE {head['removable_objects']:>4}   REVIEW {head['review_objects']:>4}"
            f"   UNKNOWN {head['unknown_objects']:>4}   KEEP {head['keep_objects']:>4}"
        )
        actionable = [f for f in findings if f.is_actionable][: args.top]
        for finding in actionable:
            print(f"    {finding.verdict:<8} {finding.key}")
            where = workspace.reports_using(name, finding.key)
            for place in where[:3]:
                print(f"             used by {place}")

    if workspace.unmatched:
        print(
            f"\n{len(workspace.unmatched)} report(s) could not be matched to a model, so "
            "their usage was not counted and no object is\nreported as removable. An "
            "unmatched report could belong to any model here, which is\nwhy every model "
            "is affected."
        )
    for warning in workspace.warnings:
        print(f"  ! {warning}")
    return 0


def _describe(args: argparse.Namespace) -> str:
    if args.pbix:
        return str(args.pbix)
    if args.pbip:
        return str(args.pbip)
    if args.rowsets:
        return f"replay of {args.rowsets}"
    return f"localhost:{args.port}" if args.port else "a local Power BI Desktop instance"


def _cmd_serve(args: argparse.Namespace) -> int:
    """Serve a source, or with none given, a page to pick one on.

    With no source flags this used to connect to Power BI Desktop. It opens the picker
    instead, where a running Desktop is one of the choices: a new user runs the bare
    command first, and a page that says what it can read beats a discovery error.
    """
    from dax_quax.render.serve import serve_switchboard

    initial = None
    if args.workspace:
        from dax_quax.sources.workspace import discover_workspace

        if args.report_folder:
            # A workspace finds its own reports by walking the directory. Adding one by
            # hand would be counted for one model and invisible to the rest, which is
            # exactly the partial coverage the workspace scan exists to remove.
            raise DaxQuaxError(
                "--report-folder does not apply to a workspace: every report under the "
                "directory is already matched to its model. Put the report in the directory."
            )
        initial = (
            "workspace",
            lambda: discover_workspace(args.workspace),
            f"workspace {args.workspace}",
        )
    elif args.pbip or args.pbix or args.rowsets or args.port:
        # A loader, not a model: the source changes while you edit it, and that is the
        # whole point of the rescan button.
        initial = ("model", lambda: _load(args), _describe(args))
    elif args.report_folder:
        raise DaxQuaxError("--report-folder needs a model: add --pbip, --pbix or --port")

    serve_switchboard(
        initial,
        thresholds=_thresholds(args),
        editor=args.editor,
        host=args.host,
        port=args.http_port,
        open_browser=not args.no_browser,
        start=str(pathlib.Path.cwd()),
    )
    return 0


def _cmd_install_runtime(args: argparse.Namespace) -> int:
    """Fetch the .NET assemblies a live connection needs.

    Not a pip dependency: they are Microsoft's, whether they may be redistributed is a
    question nobody here has answered, and they are 5 MB that only `--port` uses. A .pbip
    folder or a .pbix file needs none of it.
    """
    from dax_quax.runtime import DEFAULT_VERSION, install, user_runtime_dir

    target = args.into or user_runtime_dir()
    print(f"fetching ADOMD.NET {args.runtime_version}" + ("" if args.no_tom else " and AMO/TOM"))
    print(f"  into {target}")
    try:
        where, names = install(
            dest=target, version=args.runtime_version, with_tom=not args.no_tom
        )
    except OSError as exc:
        raise DaxQuaxError(
            f"could not install the runtime into {target}: {exc}\n"
            "Pass --into with a writable folder, or set DAXQUAX_ADOMD_DIR to one that "
            "already has the assemblies."
        ) from exc

    total = sum((where / name).stat().st_size for name in names)
    print(f"\n{len(names)} assemblies, {total / 1024 / 1024:.1f} MB")
    for name in names:
        print(f"  {name}")
    if args.runtime_version != DEFAULT_VERSION:
        print(f"\nnote: {DEFAULT_VERSION} is what this release was tested against")
    print("\nlive connections should work now:  dax-quax scan")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    """Hand this model to an agent over stdio. Read-only; see `mcp/tools.py`."""
    from dax_quax.mcp.server import serve_stdio, session_for

    if args.workspace:
        raise DaxQuaxError(
            "the MCP server takes one model at a time; a whole workspace is not served "
            "yet.\nPoint it at one model: `dax-quax mcp --pbip DIR/Name.SemanticModel`."
        )

    # A loader, not a model, for the same reason `serve` holds one: the model is usually
    # open in Power BI Desktop and changing, and the `rescan` tool has to mean something.
    # Nothing is read until the first tool call, so the client starts instantly.
    serve_stdio(
        session_for(lambda: _load(args), _describe(args), thresholds=_thresholds(args))
    )
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    from dax_quax.analysis.timings import Benchmark, compare, summarise
    from dax_quax.sources.live import discover_instances
    from dax_quax.sources.trace import benchmark as run_benchmark

    dax = args.query_file.read_text(encoding="utf-8") if args.query_file else args.query
    if not dax or not dax.strip():
        print("error: pass a DAX query, or --query-file", file=sys.stderr)
        return 1

    port = args.port
    if port is None:
        instances = discover_instances()
        if len(instances) != 1:
            # Nothing running and several running are different problems, and telling
            # someone with no instance open to pass its port sends them looking for a
            # number that does not exist.
            detail = (
                "no running Power BI Desktop instance found; open a .pbix, or pass --port"
                if not instances
                else f"found {len(instances)} running Power BI Desktop instances; "
                "pass --port to say which one"
            )
            print(f"error: {detail}", file=sys.stderr)
            return 1
        port = instances[0].port

    result = run_benchmark(
        dax, port=port, runs=args.runs, cold=args.cold, label=args.label
    )

    mode = "cold cache" if args.cold else "warm cache"
    print(f"{args.label or 'benchmark'}  ({len(result.runs)}/{args.runs} runs, {mode})")
    for note in result.errors:
        print(f"  ! {note}")
    if not result.runs:
        return 1

    print(
        f"  total  {result.median_total_ms:>6} ms median"
        f"   (best {result.best_total_ms} ms, spread {result.spread_ms} ms)"
    )
    print(f"  FE     {result.median_fe_ms:>6} ms")
    print(f"  SE     {result.median_se_ms:>6} ms over {result.se_query_count} scan(s)")

    unaccounted = {note for run in result.runs for note in run.unaccounted}
    for note in sorted(unaccounted):
        print(f"  ! {note}")

    if args.save:
        args.save.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        print(f"\nsaved to {args.save}")

    if args.compare:
        before = Benchmark.from_dict(json.loads(args.compare.read_text(encoding="utf-8")))
        delta = compare(before, result)
        print(f"\nvs {args.compare}: {delta.describe()}")
        print(f"  FE {delta.fe_ms:+} ms   SE {delta.se_ms:+} ms   scans {delta.se_queries:+}")

    if args.json:
        print(json.dumps(summarise(result), indent=2))
    return 0


def _add_editor_arg(parser: argparse.ArgumentParser) -> None:
    from dax_quax.render.editor import DEFAULT_EDITOR, EDITORS

    parser.add_argument(
        "--editor",
        choices=sorted(EDITORS),
        default=DEFAULT_EDITOR,
        help="what a file link opens. A URI nothing handles fails silently, so `none` "
        f"keeps the path as text and links nothing (default: {DEFAULT_EDITOR})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dax-quax", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="print findings, or emit them as JSON")
    _add_source_args(scan)
    scan.add_argument("--json", action="store_true", help="emit JSON for a pipeline")
    scan.add_argument("--top", type=int, default=10, help="how many findings to list")
    memory = scan.add_argument_group("baseline")
    memory.add_argument(
        "--save", type=pathlib.Path, metavar="FILE", help="write this scan as a baseline"
    )
    memory.add_argument(
        "--compare",
        type=pathlib.Path,
        metavar="FILE",
        help="a saved baseline to compare against, for what changed since",
    )
    gate = scan.add_argument_group(
        "CI gates (need --compare; a gate that cannot be evaluated fails)"
    )
    gate.add_argument(
        "--fail-on-new-remove",
        action="store_true",
        help="exit non-zero if any object newly reports REMOVE",
    )
    gate.add_argument(
        "--max-growth-mb",
        type=float,
        metavar="N",
        help="exit non-zero if the model grew by more than N MB",
    )
    gate.add_argument(
        "--fail-on-scope-loss",
        action="store_true",
        help="exit non-zero if this scan can defend fewer verdicts than the baseline, "
        "which is what a deleted or renamed report looks like",
    )
    scan.set_defaults(func=_cmd_scan)

    report = sub.add_parser("report", help="write a self-contained HTML report")
    _add_source_args(report)
    report.add_argument(
        "-o", "--out", type=pathlib.Path, default=pathlib.Path("dax-quax-report.html")
    )
    _add_editor_arg(report)
    report.set_defaults(func=_cmd_report)

    serve = sub.add_parser("serve", help="serve the report, with rescan and lineage")
    _add_source_args(serve)
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost)")
    serve.add_argument(
        "--http-port", type=int, default=8777, help="port to serve on (default: 8777)"
    )
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser")
    _add_editor_arg(serve)
    serve.set_defaults(func=_cmd_serve)

    bench = sub.add_parser(
        "bench",
        help="time a DAX query against a live instance, and compare against a saved run",
    )
    bench.add_argument("query", nargs="?", help="the DAX query to time")
    bench.add_argument("--query-file", type=pathlib.Path, help="read the query from a file")
    bench.add_argument("--port", type=int, help="port of a running Power BI Desktop instance")
    bench.add_argument("--runs", type=int, default=3, help="how many times to run it")
    bench.add_argument(
        "--cold",
        action="store_true",
        help="clear the storage-engine cache before every run. Without this the first run "
        "warms the cache and the rest measure the cache, not the model",
    )
    bench.add_argument("--label", help="a name for this run, shown in the output")
    bench.add_argument("--save", type=pathlib.Path, help="write the result as JSON")
    bench.add_argument(
        "--compare", type=pathlib.Path, metavar="BEFORE.json",
        help="a saved run to compare against, for a before/after",
    )
    bench.add_argument("--json", action="store_true", help="also emit a JSON summary")
    bench.set_defaults(func=_cmd_bench)

    runtime = sub.add_parser(
        "install-runtime",
        help="fetch the .NET assemblies a live connection needs (not needed for "
        ".pbip or .pbix)",
    )
    runtime.add_argument(
        "--into",
        type=pathlib.Path,
        metavar="DIR",
        help="where to put them (default: a per-user folder, not site-packages)",
    )
    runtime.add_argument(
        "--runtime-version",
        default="19.84.1",
        help="ADOMD.NET version (default: 19.84.1, what this release was tested against)",
    )
    runtime.add_argument(
        "--no-tom",
        action="store_true",
        help="skip AMO/TOM, saving 4.5 MB. `bench` needs it for tracing; nothing else does",
    )
    runtime.set_defaults(func=_cmd_install_runtime)

    agent = sub.add_parser(
        "mcp",
        help="run a read-only MCP server over this model, for an agent to query",
    )
    _add_source_args(agent)
    agent.set_defaults(func=_cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DaxQuaxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
