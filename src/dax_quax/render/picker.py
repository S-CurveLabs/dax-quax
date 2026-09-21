"""Choosing what `serve` scans from the page, rather than on the command line.

A browser cannot hand a local path to a server: a file input uploads bytes and hides the
path, and a `.pbip` project is a folder anyway. So the page browses the disk through the
server, the way Jupyter does, and this module is the part of that with no web framework
in it — what a folder contains, and what picking something in it means.

WHAT A PICK MEANS
-----------------
Folders scan as a **workspace**: every model under it, against every report under it.
That is the default on purpose. Picking a model and then ticking its reports one by one
is exactly how a report gets left out, and a report left out is a false REMOVE.

A project (a `.pbip` file or a `.SemanticModel` folder) is offered only where it is the
only model in its folder. `open_pbip` attaches every sibling `.Report`, so beside a
second model it would count reports that belong to someone else — the page offers the
folder instead.

A drive root or the home folder is refused as a workspace. Scanning one walks every
directory under it, which looks exactly like the server having hung.
"""

from __future__ import annotations

import os
import pathlib
import string
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from dax_quax.errors import DaxQuaxError

__all__ = ["FILE_SUFFIXES", "MODEL_SUFFIXES", "Choice", "choose", "listing", "roots"]

MODEL_SUFFIXES = (".SemanticModel", ".Dataset")
FILE_SUFFIXES = (".pbix", ".abf", ".xlsx")

#: A listing longer than this is cut short and says so. A folder with tens of thousands
#: of entries is not where a model lives, and drawing all of them freezes the page.
MAX_ENTRIES = 2000

Kind = Literal["workspace", "project", "file", "live"]


@dataclass(frozen=True, slots=True)
class Choice:
    """A source picked on the page: which app shape serves it, and how to load it."""

    shape: Literal["model", "workspace"]
    loader: Callable[[], Any]
    description: str


def roots() -> list[str]:
    """Where browsing can start: the drives (or /), then the home folder."""
    home = str(pathlib.Path.home())
    if sys.platform == "win32":
        # A and B are floppy letters; probing them can stall for seconds.
        drives = [f"{letter}:\\" for letter in string.ascii_uppercase[2:]]
        return [d for d in drives if os.path.isdir(d)] + [home]
    return ["/", home]


def _kind(entry: os.DirEntry) -> str | None:
    name = entry.name
    try:
        if entry.is_dir():
            if name.endswith(MODEL_SUFFIXES):
                return "model"
            if name.endswith(".Report"):
                return "report"
            return "dir"
        lower = name.lower()
        if lower.endswith(".pbip"):
            return "pbip"
        if lower.endswith(FILE_SUFFIXES):
            return "file"
    except OSError:
        return None
    return None


def _too_broad(path: pathlib.Path) -> bool:
    return path == pathlib.Path(path.anchor) or path == pathlib.Path.home().resolve()


def listing(path: str | os.PathLike[str]) -> dict[str, Any]:
    """What a folder holds that could be scanned, plus the folders to walk into.

    Hidden entries (a leading `.` or `$`) are left out: `.pbi` caches, `.git` and
    `$Recycle.Bin` are never what someone is looking for.
    """
    folder = pathlib.Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise DaxQuaxError(f"not a folder: {folder}")

    entries: list[dict[str, str]] = []
    truncated = False
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.name.startswith((".", "$")):
                    continue
                kind = _kind(entry)
                if kind is None:
                    continue
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                entries.append({"name": entry.name, "path": entry.path, "kind": kind})
    except PermissionError as exc:
        raise DaxQuaxError(f"cannot read {folder}: permission denied") from exc

    # Folders first, then files; each alphabetical the way Explorer shows them.
    entries.sort(key=lambda e: (e["kind"] in ("pbip", "file"), e["name"].lower()))
    parent = folder.parent
    return {
        "path": str(folder),
        "parent": str(parent) if parent != folder else None,
        "entries": entries,
        "models_here": sum(e["kind"] == "model" for e in entries),
        "truncated": truncated,
        "scannable": not _too_broad(folder),
    }


def choose(kind: str, path: str | None = None, port: int | None = None) -> Choice:
    """Turn what was picked into a loader. Validates, but loads nothing yet."""
    if kind == "live":
        from dax_quax import connect

        where = f"localhost:{port}" if port else "a local Power BI Desktop instance"
        return Choice("model", lambda: connect(port=port), where)

    if not path:
        raise DaxQuaxError(f"a {kind} scan needs a path")
    target = pathlib.Path(path).expanduser().resolve()
    if not target.exists():
        raise DaxQuaxError(f"nothing at {target}")

    if kind == "workspace":
        if not target.is_dir():
            raise DaxQuaxError(f"not a folder: {target}")
        if _too_broad(target):
            raise DaxQuaxError(
                f"scanning all of {target} would walk every folder under it. "
                "Pick the folder that holds your projects."
            )
        from dax_quax.sources.workspace import discover_workspace

        return Choice("workspace", lambda: discover_workspace(target), f"workspace {target}")

    if kind == "project":
        is_model_dir = target.is_dir() and target.name.endswith(MODEL_SUFFIXES)
        is_pbip = target.is_file() and target.suffix.lower() == ".pbip"
        if not (is_model_dir or is_pbip):
            raise DaxQuaxError(f"not a .pbip file or a semantic model folder: {target}")
        # The page hides the button in this case; a pasted path does not go through it.
        siblings = [
            p for p in target.parent.iterdir() if p.is_dir() and p.name.endswith(MODEL_SUFFIXES)
        ]
        if len(siblings) > 1:
            raise DaxQuaxError(
                f"{target.parent} holds {len(siblings)} semantic models, so a project scan "
                "would count the other models' reports as this one's. Scan the folder."
            )
        from dax_quax.sources.pbip import open_pbip

        return Choice("model", lambda: open_pbip(target), str(target))

    if kind == "file":
        if not (target.is_file() and target.suffix.lower() in FILE_SUFFIXES):
            raise DaxQuaxError(f"not a .pbix, .abf or .xlsx file: {target}")

        # load_file names the extra to install when pbixray is missing.
        from dax_quax.sources.file import load_file

        return Choice("model", lambda: load_file(target), str(target))

    raise DaxQuaxError(f"unknown kind of source: {kind!r}")
