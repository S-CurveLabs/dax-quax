"""Every module must import with no optional dependency installed, bar the ones declared here.

    python tools/check_imports.py

A stray top-level `import pbixray` or `from fastapi import ...` makes the whole library
unimportable for anyone who did not install that extra. The library is the product
(CONVENTIONS §3), so that is a packaging bug, and it is invisible on a development machine
where every extra is present.

HOW THIS RUNS ANYWHERE
----------------------
The obvious way to check is to install the package bare and import everything, which is
what CI does. But then the check only works in CI, and nobody runs it before pushing. So
this blocks the optional packages itself, in a subprocess, and gives the same answer on a
machine that has them all installed.

CI still installs bare as well, because the two catch different things: blocking catches a
stray import of a *known* optional package, and a bare install also catches a dependency
somebody added to the core requirements without declaring it.

THE ALLOWANCES ARE CHECKED TOO
------------------------------
A module listed in NEEDS_EXTRA must actually fail without its package. If it starts
importing cleanly the allowance is stale and this fails, because a list of exceptions
nobody re-examines is how `sources/live.py` came to be skipped here for months while it
imported perfectly well on Linux -- so a top-level pythonnet import in it would not have
been caught by the job that exists to catch exactly that.
"""

from __future__ import annotations

import json
import subprocess
import sys

#: Packages behind an extra in pyproject.toml. Blocked, to simulate a bare install.
OPTIONAL = (
    "clr",
    "clr_loader",
    "pythonnet",
    "fastapi",
    "uvicorn",
    "starlette",
    "jinja2",
    "pbixray",
    "mcp",
)

#: Module -> the package it is allowed to need at import time, and why.
NEEDS_EXTRA: dict[str, str] = {
    # FastAPI resolves a route's annotations against the module namespace, and with
    # `from __future__ import annotations` every annotation is a string. A Request
    # imported inside create_app is invisible to it, and `request: Request` silently
    # becomes a required query parameter -- a 422 on every page. So the import is at
    # module scope deliberately, and only `serve` imports this module.
    "dax_quax.render.serve": "fastapi",
}

_PROBE = """
import importlib, json, pkgutil, sys

BLOCK = set(json.loads({blocked!r}))

class Block:
    # find_spec, not find_module: the latter was removed in 3.12 and is never consulted.
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError(name)
        return None

sys.meta_path.insert(0, Block())

import dax_quax

result = {{}}
for module in pkgutil.walk_packages(dax_quax.__path__, "dax_quax."):
    try:
        importlib.import_module(module.name)
        result[module.name] = None
    except Exception as exc:
        result[module.name] = f"{{type(exc).__name__}}: {{exc}}"
print("@@" + json.dumps(result))
"""


def probe() -> dict[str, str | None]:
    """Import every module with the optional packages blocked. None means it imported."""
    script = _PROBE.format(blocked=json.dumps(list(OPTIONAL)))
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or done.stdout.strip())
    line = next(x for x in done.stdout.splitlines() if x.startswith("@@"))
    return json.loads(line[2:])


def analyse(outcome: dict[str, str | None], allowances: dict[str, str] | None = None) -> list[str]:
    """Everything wrong with this outcome, as lines a reader can act on."""
    allowances = NEEDS_EXTRA if allowances is None else allowances
    problems: list[str] = []

    for module, error in sorted(outcome.items()):
        allowed = allowances.get(module)
        if error is None:
            if allowed:
                problems.append(
                    f"{module}: imports fine without {allowed!r}, so its entry in "
                    "NEEDS_EXTRA is stale. Remove it."
                )
            continue
        if allowed is None:
            problems.append(f"{module}: {error}")
        elif allowed not in error:
            problems.append(
                f"{module}: allowed to need {allowed!r} but failed with a different "
                f"problem -- {error}"
            )

    for module in allowances:
        if module not in outcome:
            problems.append(f"{module}: listed in NEEDS_EXTRA but no such module")

    return problems


def run() -> int:
    try:
        outcome = probe()
    except RuntimeError as exc:
        print(f"could not import dax_quax at all: {exc}", file=sys.stderr)
        return 1

    problems = analyse(outcome)
    checked = len(outcome)
    if problems:
        print(f"{checked} modules checked, {len(problems)} problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    allowed = ", ".join(sorted(NEEDS_EXTRA)) or "none"
    print(f"{checked} modules import with no extras installed. Allowed to need one: {allowed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
