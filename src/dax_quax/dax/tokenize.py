"""A DAX scanner. Correct about, and only about, the things that hide references.

This is deliberately not a parser. It knows nothing of precedence, arity or types. It knows
comments, string literals, quoted identifiers, bracketed identifiers and their escapes —
because those are exactly the constructs that make a regex over DAX wrong:

    "this [Column] is inside a string"   -- not a reference
    // Sales[Amount] in a comment        -- not a reference
    'It''s Complicated'[Value]           -- a quote inside a table name
    Sales[Total ]] Weird]                -- a bracket inside a column name

Never raises on malformed input. An unterminated literal yields a token with
``terminated=False`` so the caller can decide, because refusing to analyse a model because
one measure has a typo would be useless.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Token", "TokenKind", "significant", "tokenize"]


class TokenKind(StrEnum):
    IDENT = "ident"  # bare word: table name, function name, keyword, variable
    QUOTED = "quoted"  # 'Table Name'
    BRACKET = "bracket"  # [Column] or [Measure]
    STRING = "string"  # "literal"
    NUMBER = "number"
    PUNCT = "punct"
    COMMENT = "comment"
    WHITESPACE = "whitespace"


TRIVIA = frozenset({TokenKind.COMMENT, TokenKind.WHITESPACE})

#: Two-character operators, checked before single characters.
_DIGRAPHS = ("&&", "||", "<>", "<=", ">=", "^=", ":=")


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str  # decoded for QUOTED/BRACKET/STRING; raw otherwise
    start: int
    end: int
    terminated: bool = True

    @property
    def is_trivia(self) -> bool:
        return self.kind in TRIVIA


def tokenize(text: str) -> Iterator[Token]:
    """Yield every token in ``text``, trivia included."""
    i, n = 0, len(text)

    while i < n:
        char = text[i]

        # -- whitespace ---------------------------------------------------------------
        if char.isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            yield Token(TokenKind.WHITESPACE, text[start:i], start, i)
            continue

        # -- comments -----------------------------------------------------------------
        # DAX accepts // and -- for line comments. '--' means a subtraction only when
        # something separates the two signs, so treating it as a comment matches the
        # engine (and DAX Formatter).
        if text.startswith(("//", "--"), i):
            start = i
            while i < n and text[i] not in "\r\n":
                i += 1
            yield Token(TokenKind.COMMENT, text[start:i], start, i)
            continue

        if text.startswith("/*", i):
            start = i
            close = text.find("*/", i + 2)
            i = n if close == -1 else close + 2
            yield Token(TokenKind.COMMENT, text[start:i], start, i, terminated=close != -1)
            continue

        # -- delimited literals -------------------------------------------------------
        if char == '"':
            start = i
            value, i, closed = _delimited(text, i, '"', '"')
            yield Token(TokenKind.STRING, value, start, i, terminated=closed)
            continue

        if char == "'":
            start = i
            value, i, closed = _delimited(text, i, "'", "'")
            yield Token(TokenKind.QUOTED, value, start, i, terminated=closed)
            continue

        if char == "[":
            start = i
            value, i, closed = _delimited(text, i, "[", "]")
            yield Token(TokenKind.BRACKET, value, start, i, terminated=closed)
            continue

        # -- numbers ------------------------------------------------------------------
        if char.isdigit() or (char == "." and i + 1 < n and text[i + 1].isdigit()):
            start = i
            while i < n and (text[i].isdigit() or text[i] == "."):
                i += 1
            if i < n and text[i] in "eE":
                probe = i + 1
                if probe < n and text[probe] in "+-":
                    probe += 1
                if probe < n and text[probe].isdigit():
                    i = probe
                    while i < n and text[i].isdigit():
                        i += 1
            yield Token(TokenKind.NUMBER, text[start:i], start, i)
            continue

        # -- bare identifiers ---------------------------------------------------------
        if char.isalpha() or char == "_":
            start = i
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            yield Token(TokenKind.IDENT, text[start:i], start, i)
            continue

        # -- punctuation --------------------------------------------------------------
        if text.startswith(_DIGRAPHS, i):
            yield Token(TokenKind.PUNCT, text[i : i + 2], i, i + 2)
            i += 2
            continue

        yield Token(TokenKind.PUNCT, char, i, i + 1)
        i += 1


def _delimited(text: str, start: int, open_char: str, close_char: str) -> tuple[str, int, bool]:
    """Scan a delimited literal, decoding doubled close characters as escapes.

    Returns the decoded contents, the index just past the closing delimiter, and whether a
    closing delimiter was actually found.
    """
    i = start + 1
    n = len(text)
    parts: list[str] = []
    while i < n:
        char = text[i]
        if char == close_char:
            if i + 1 < n and text[i + 1] == close_char:  # doubled: an escaped delimiter
                parts.append(close_char)
                i += 2
                continue
            return "".join(parts), i + 1, True
        parts.append(char)
        i += 1
    return "".join(parts), n, False


def significant(text: str) -> list[Token]:
    """Every token that is not whitespace or a comment."""
    return [token for token in tokenize(text) if not token.is_trivia]
