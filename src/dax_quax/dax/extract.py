"""Pull candidate object references out of a DAX expression.

Candidates are *syntactic*. Nothing here knows what exists in a model — that is resolve.py's
job, and keeping the two apart is what lets the extractor be tested on its own.

Three shapes come out:

    Sales[Amount]     qualified_column   an unambiguous table-qualified reference
    [Total Sales]     bare_bracket       a measure, or a column of the surrounding table
    ALL ( Sales )     bare_table         a bare identifier that is not a function or keyword

Variables are tracked so ``VAR Revenue = ...  RETURN Revenue`` never reports a table named
Revenue, and function names are excluded because ``SUM (`` is not a table called SUM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dax_quax.dax.tokenize import Token, TokenKind, significant

__all__ = ["Candidate", "Extraction", "KEYWORDS", "extract"]

CandidateKind = Literal["qualified_column", "bare_bracket", "bare_table"]

#: Reserved words that are never an object reference. Case-insensitive.
KEYWORDS = frozenset(
    {
        "VAR", "RETURN", "DEFINE", "EVALUATE", "MEASURE", "COLUMN", "TABLE", "ORDER", "BY",
        "START", "AT", "ASC", "DESC", "IN", "NOT", "AND", "OR", "TRUE", "FALSE", "BLANK",
        "DETAILROWS", "WITH", "VISUAL", "SHAPE", "ROW",
    }
)


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: CandidateKind
    name: str
    table: str | None = None
    position: int = 0

    def __str__(self) -> str:
        if self.kind == "qualified_column":
            return f"{self.table}[{self.name}]"
        if self.kind == "bare_bracket":
            return f"[{self.name}]"
        return self.name


@dataclass(frozen=True, slots=True)
class Extraction:
    candidates: tuple[Candidate, ...] = ()
    variables: frozenset[str] = frozenset()
    functions: frozenset[str] = frozenset()
    malformed: tuple[str, ...] = ()

    def of_kind(self, kind: CandidateKind) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.kind == kind)


def extract(expression: str | None) -> Extraction:
    """Find every candidate reference in one DAX expression."""
    if not expression or not expression.strip():
        return Extraction()

    tokens = significant(expression)
    candidates: list[Candidate] = []
    variables: set[str] = set()
    functions: set[str] = set()
    malformed: list[str] = []

    expecting_variable_name = False

    for index, token in enumerate(tokens):
        if not token.terminated:
            malformed.append(f"unterminated {token.kind.value} at offset {token.start}")

        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None

        # -- [something] ---------------------------------------------------------------
        if token.kind is TokenKind.BRACKET:
            # An identifier before a bracket is the qualifying table, even with whitespace
            # between them — 'Sales [Amount]' is legal DAX. A comma or paren in between
            # breaks the pairing, which is why SUMX(Sales, [Amount]) reads as a table plus
            # an unqualified reference rather than as one column.
            qualified = (
                previous is not None
                and previous.kind in (TokenKind.IDENT, TokenKind.QUOTED)
                and not _is_keyword(previous)
            )
            if qualified:
                assert previous is not None
                candidates.append(
                    Candidate("qualified_column", token.value, previous.value, token.start)
                )
            else:
                candidates.append(Candidate("bare_bracket", token.value, None, token.start))
            continue

        if token.kind is not TokenKind.IDENT and token.kind is not TokenKind.QUOTED:
            continue

        # -- VAR <name> ----------------------------------------------------------------
        if expecting_variable_name:
            variables.add(token.value.casefold())
            expecting_variable_name = False
            continue

        if _is_keyword(token):
            if token.value.upper() == "VAR":
                expecting_variable_name = True
            continue

        # -- function call ---------------------------------------------------------------
        if following is not None and following.kind is TokenKind.PUNCT and following.value == "(":
            functions.add(token.value.upper())
            continue

        # -- the qualifier of a following bracket; already handled above -----------------
        if following is not None and following.kind is TokenKind.BRACKET:
            continue

        # -- a variable reference --------------------------------------------------------
        if token.value.casefold() in variables:
            continue

        candidates.append(Candidate("bare_table", token.value, None, token.start))

    return Extraction(
        candidates=tuple(candidates),
        variables=frozenset(variables),
        functions=frozenset(functions),
        malformed=tuple(malformed),
    )


def _is_keyword(token: Token) -> bool:
    # A quoted identifier is never a keyword: 'Table' is a table even though TABLE is one.
    return token.kind is TokenKind.IDENT and token.value.upper() in KEYWORDS
