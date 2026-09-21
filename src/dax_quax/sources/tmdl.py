"""A TMDL block reader.

TMDL is indentation-structured text, one file per model object. This turns it into a tree
of nodes and stops there — mapping nodes onto a Model is pbip.py's job, exactly as
tokenize.py stops short of knowing what a model contains.

    table Sales
        column 'Order Number'
            dataType: string
            isHidden
        measure 'Total Sales' = SUMX ( Sales, Sales[Line Amount] )
            formatString: 0

Three line shapes matter:

    keyword name            opens a child block
    keyword name = value    opens a block whose value is an expression
    property: value         a property of the enclosing block
    property                a bare boolean property, meaning true

ON THE ACCURACY OF THIS FILE
----------------------------
Like the DMV contract in dmv.py, this encodes assumptions about a format that has not been
checked against output from a real Power BI Desktop. Anything the reader does not
understand is collected in ``TmdlDocument.unparsed`` rather than dropped, so a wrong
assumption is visible instead of silently costing a column its lineage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["TmdlDocument", "TmdlNode", "parse_tmdl", "split_reference", "unquote"]

#: Keywords that open an object block. Anything else at block position is recorded as
#: unparsed rather than guessed at.
BLOCK_KEYWORDS = frozenset(
    {
        "model", "table", "column", "measure", "partition", "relationship", "hierarchy",
        "level", "role", "tablePermission", "perspective", "calculationGroup",
        "calculationItem", "expression", "annotation", "extendedProperty", "queryGroup",
        "variation", "changedProperty", "ref", "detailRowsDefinition",
        # Corrected against real Power BI Desktop output (see CONVENTIONS §10). `culture`
        # was a guess and the keyword is `cultureInfo`; the rest were simply missing.
        # `kpi` is the costly one: it is written inside a table file, which the loader
        # does read, and its status/trend/target expressions are DAX.
        "kpi", "cultureInfo", "perspectiveTable", "perspectiveColumn", "perspectiveMeasure",
        "database",
    }
)

#: `ref table Calendar` — a reference block whose name is two words, not one. Only
#: perspectives and the model file use it, and the plain block regex cannot match it.
_REF = re.compile(r"^ref\s+(?P<kind>[A-Za-z_][\w]*)\s+(?P<name>'(?:[^']|'')*'|\S+)$")

_DESCRIPTION = "///"
_FENCE = "```"


@dataclass(slots=True)
class TmdlNode:
    keyword: str
    name: str
    value: str | None = None  # the right-hand side of '=', inline or multi-line
    properties: dict[str, str] = field(default_factory=dict)
    children: list[TmdlNode] = field(default_factory=list)
    description: str | None = None
    line: int = 0

    def find(self, keyword: str) -> list[TmdlNode]:
        return [child for child in self.children if child.keyword == keyword]

    def first(self, keyword: str) -> TmdlNode | None:
        for child in self.children:
            if child.keyword == keyword:
                return child
        return None

    def flag(self, name: str) -> bool:
        return self.properties.get(name, "").strip().lower() in {"", "true", "1"} and (
            name in self.properties
        )

    def prop(self, name: str) -> str | None:
        value = self.properties.get(name)
        return value.strip() if value else None


@dataclass(slots=True)
class TmdlDocument:
    nodes: list[TmdlNode] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)

    @property
    def root(self) -> TmdlNode | None:
        return self.nodes[0] if self.nodes else None


def unquote(text: str) -> str:
    """Strip TMDL name quoting, decoding the doubled-quote escape."""
    text = text.strip()
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    return text


def split_reference(text: str) -> tuple[str | None, str] | None:
    """Split ``Sales.'Product Key'`` into its table and column parts.

    Returns None when the text is not a dotted reference at all, so the caller can report
    it rather than inventing a table name.
    """
    text = text.strip()
    if not text:
        return None
    parts: list[str] = []
    current = ""
    in_quote = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if in_quote and index + 1 < len(text) and text[index + 1] == "'":
                current += "'"
                index += 2
                continue
            in_quote = not in_quote
            index += 1
            continue
        if char == "." and not in_quote:
            parts.append(current)
            current = ""
            index += 1
            continue
        current += char
        index += 1
    parts.append(current)
    if len(parts) == 1:
        return (None, parts[0].strip())
    return (parts[0].strip(), ".".join(parts[1:]).strip())


def _indent(line: str) -> int:
    """Indent depth in levels. A tab is one level; four spaces are one level."""
    width = 0
    for char in line:
        if char == "\t":
            width += 4
        elif char == " ":
            width += 1
        else:
            break
    return width // 4


_PROPERTY = re.compile(r"^(?P<name>[A-Za-z_][\w]*)\s*:\s*(?P<value>.*)$")
#: Properties whose value is an expression use '=' rather than ':' — a partition's
#: `source =`, for one. Missing this form leaves whole M queries reported as unparsed.
_PROPERTY_EXPR = re.compile(r"^(?P<name>[A-Za-z_][\w]*)\s*=\s*(?P<value>.*)$")
_BLOCK = re.compile(
    r"^(?P<keyword>[A-Za-z_][\w]*)\s+(?P<name>'(?:[^']|'')*'|[^\s=]+)\s*(?:=\s*(?P<value>.*))?$"
)
_BARE_FLAG = re.compile(r"^(?P<name>[A-Za-z_][\w]*)$")


def parse_tmdl(text: str) -> TmdlDocument:
    """Parse one .tmdl file into a node tree. Never raises on malformed input."""
    document = TmdlDocument()
    lines = text.splitlines()
    stack: list[tuple[int, TmdlNode]] = []
    pending_description: list[str] = []
    index = 0

    while index < len(lines):
        raw = lines[index]
        index += 1
        stripped = raw.strip()

        if not stripped:
            continue
        if stripped.startswith(_DESCRIPTION):
            pending_description.append(stripped[len(_DESCRIPTION) :].strip())
            continue

        depth = _indent(raw)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else None

        bare = _BARE_FLAG.match(stripped)
        if bare and bare["name"] in BLOCK_KEYWORDS:
            # A nameless block: `calculationGroup` opens one, `isHidden` does not.
            node = TmdlNode(
                keyword=bare["name"],
                name="",
                description=" ".join(pending_description) or None,
                line=index,
            )
            pending_description = []
            if parent is None:
                document.nodes.append(node)
            else:
                parent.children.append(node)
            stack.append((depth, node))
            continue

        reference = _REF.match(stripped)
        if reference:
            # `ref table Calendar` inside a perspective. Recorded as a `ref` node whose
            # value is the kind, so a caller can tell a table ref from a measure ref.
            node = TmdlNode(
                keyword="ref",
                name=unquote(reference["name"]),
                value=reference["kind"],
                line=index,
            )
            pending_description = []
            if parent is None:
                document.nodes.append(node)
            else:
                parent.children.append(node)
            continue

        block = _BLOCK.match(stripped)
        if block and block["keyword"] in BLOCK_KEYWORDS:
            value = block["value"]
            if value is not None and value.strip() == "":
                value, index = _read_multiline(lines, index, depth + 1)
            elif value is not None and value.strip().startswith(_FENCE):
                value, index = _read_fenced(lines, index)
            node = TmdlNode(
                keyword=block["keyword"],
                name=unquote(block["name"]),
                value=value.strip() if value else None,
                description=" ".join(pending_description) or None,
                line=index,
            )
            pending_description = []
            if parent is None:
                document.nodes.append(node)
            else:
                parent.children.append(node)
            stack.append((depth, node))
            continue

        pending_description = []

        if parent is not None:
            prop = _PROPERTY.match(stripped) or _PROPERTY_EXPR.match(stripped)
            if prop:
                value = prop["value"]
                if value.strip() == "":
                    value, index = _read_multiline(lines, index, depth)
                elif value.strip().startswith(_FENCE):
                    value, index = _read_fenced(lines, index)
                parent.properties[prop["name"]] = value.strip()
                continue
            flag = _BARE_FLAG.match(stripped)
            if flag:
                parent.properties[flag["name"]] = "true"
                continue

        document.unparsed.append(f"line {index}: {stripped[:100]}")

    return document


def _read_multiline(lines: list[str], index: int, floor: int) -> tuple[str, int]:
    """Collect the continuation lines of a value that opened with a bare '=' or ':'.

    ``floor`` is the depth a continuation line must beat, and it differs by what opened
    the value. A block (``measure Margin =``) has its own properties one level in, so its
    body has to be deeper than those. A property (``statusExpression =``) has its siblings
    at its own level, so its body is exactly one level in.

    Passing the block's floor for a property is how a KPI's status and trend expressions
    came back empty against real Power BI output: the body sat at exactly the depth this
    stopped at, so every line of DAX in it fell through as unparsed.
    """
    collected: list[str] = []
    while index < len(lines):
        raw = lines[index]
        if not raw.strip():
            collected.append("")
            index += 1
            continue
        if _indent(raw) <= floor:
            break
        collected.append(raw.strip())
        index += 1
    while collected and not collected[-1]:
        collected.pop()
    return "\n".join(collected), index


def _read_fenced(lines: list[str], index: int) -> tuple[str, int]:
    collected: list[str] = []
    while index < len(lines):
        raw = lines[index]
        index += 1
        if raw.strip().startswith(_FENCE):
            break
        collected.append(raw.strip())
    return "\n".join(collected), index
