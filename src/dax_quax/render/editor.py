"""Turn a `SourceRef` into something clickable.

A finding says an object can go. The file that defines it is the next thing you need, and
until now the reader had to go find it. A .pbip project is a code project — that is the
entire point of Power BI Project mode — so the useful destination is an editor, not a
viewer.

WHY THIS IS A CHOICE AND NOT A CONSTANT
---------------------------------------
An editor URI that nothing on the machine handles fails silently: the click does nothing
and there is no way to tell that from a broken link. So the path is always rendered as
readable text whether or not it is also a link, the scheme is selectable, and `none`
turns the linking off entirely while keeping the text.

`file` is the honest fallback. It opens in the browser rather than an editor, which reads
a .tmdl but does not let you change it — still better than nothing when the alternative is
a dead click.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dax_quax.model import SourceRef

__all__ = ["DEFAULT_EDITOR", "EDITORS", "editor_uri"]

#: Scheme name -> URI template. `{path}` is absolute with forward slashes, which is what
#: every one of these expects, including on Windows (vscode://file/C:/Users/...).
EDITORS: dict[str, str] = {
    "vscode": "vscode://file/{path}:{line}",
    "vscode-insiders": "vscode-insiders://file/{path}:{line}",
    "cursor": "cursor://file/{path}:{line}",
    "file": "file:///{path}",
    "none": "",
}

DEFAULT_EDITOR = "vscode"


def editor_uri(ref: SourceRef | None, editor: str = DEFAULT_EDITOR) -> str | None:
    """A URI for this location, or None when there is nothing worth linking to."""
    if ref is None:
        return None
    template = EDITORS.get(editor, "")
    if not template:
        return None
    path = ref.path.as_posix()
    # A line of 0 would send an editor to the top of the file and look like a miss.
    return template.format(path=path, line=ref.line or 1)
