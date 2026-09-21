"""The DAX layer: a reference extractor, deliberately not a parser.

    tokenize -> extract (syntactic candidates) -> resolve (against a Model)

See CONVENTIONS.md section 6 for why this stops short of an AST.
"""

from dax_quax.dax.extract import Candidate, Extraction, extract
from dax_quax.dax.resolve import (
    ModelIndex,
    Reference,
    Resolution,
    Unresolved,
    references_in,
    resolve,
)
from dax_quax.dax.tokenize import Token, TokenKind, tokenize

__all__ = [
    "Candidate",
    "Extraction",
    "ModelIndex",
    "Reference",
    "Resolution",
    "Token",
    "TokenKind",
    "Unresolved",
    "extract",
    "references_in",
    "resolve",
    "tokenize",
]
