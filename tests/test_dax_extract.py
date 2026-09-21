"""Tokenizer and extractor tests.

Most of these are the cases that make a regex over DAX wrong. If one of them regresses, the
lineage graph silently loses or invents edges, and M4 then reports a column as unused when
it is not.
"""

from __future__ import annotations

import pytest

from dax_quax.dax.extract import extract
from dax_quax.dax.tokenize import TokenKind, significant, tokenize


def kinds(expression: str) -> list[TokenKind]:
    return [t.kind for t in significant(expression)]


def values(expression: str, kind: TokenKind) -> list[str]:
    return [t.value for t in significant(expression) if t.kind is kind]


# -- tokenizer ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "// Sales[Amount]",
        "-- Sales[Amount]",
        "/* Sales[Amount] */",
        "/* unterminated Sales[Amount]",
    ],
)
def test_comments_hide_everything_inside_them(expression):
    assert significant(expression) == []


def test_line_comment_ends_at_the_newline():
    assert values("// hidden\nSales[Amount]", TokenKind.BRACKET) == ["Amount"]


def test_string_escapes_decode():
    assert values('"say ""hello"" now"', TokenKind.STRING) == ['say "hello" now']


def test_quoted_identifier_escapes_decode():
    assert values("'It''s Complicated'[Value]", TokenKind.QUOTED) == ["It's Complicated"]


def test_bracket_escapes_decode():
    """A column name may contain a closing bracket, doubled."""
    assert values("Sales[Total ]] Weird]", TokenKind.BRACKET) == ["Total ] Weird"]


def test_double_minus_is_a_comment_even_without_spaces():
    """`Sales[A]--1` comments out the rest of the line; it is not a double negative.

    Surprising, but it is what the engine does, so the extractor must agree with it.
    """
    found = extract("Sales[A]--1 + Sales[B]").candidates
    assert [str(c) for c in found] == ["Sales[A]"]


def test_block_comments_do_not_nest():
    """The first `*/` closes the comment, so what follows is real code."""
    assert [str(c) for c in extract("/* outer /* inner */ Sales[Real]").candidates] == [
        "Sales[Real]"
    ]


def test_unterminated_literal_is_flagged_not_raised():
    tokens = significant('Sales[Amount] & "oops')
    assert tokens[-1].kind is TokenKind.STRING
    assert tokens[-1].terminated is False


def test_numbers_including_exponents():
    assert values("1 1.5 .5 1e3 2.5E-4", TokenKind.NUMBER) == ["1", "1.5", ".5", "1e3", "2.5E-4"]


def test_digraph_operators_stay_together():
    assert values("a && b || c <> d", TokenKind.PUNCT) == ["&&", "||", "<>"]


def test_token_offsets_survive_escape_decoding():
    """Offsets index the source, not the decoded value — needed for error reporting."""
    expression = '"a ""b"" c" + 1'
    token = next(t for t in tokenize(expression) if t.kind is TokenKind.STRING)
    assert expression[token.start : token.end] == '"a ""b"" c"'


# -- extraction: what counts as a reference ------------------------------------------------


def test_qualified_column():
    found = extract("Sales[Amount]").candidates
    assert len(found) == 1
    assert (found[0].kind, found[0].table, found[0].name) == ("qualified_column", "Sales", "Amount")


def test_quoted_table_qualifier():
    found = extract("'Sales Header'[Amount]").candidates
    assert (found[0].table, found[0].name) == ("Sales Header", "Amount")


def test_whitespace_between_table_and_bracket_still_qualifies():
    found = extract("Sales [Amount]").candidates
    assert found[0].kind == "qualified_column"


def test_a_comma_breaks_the_pairing():
    """SUMX(Sales, [Amount]) is a table plus an unqualified reference, not one column."""
    found = extract("SUMX ( Sales, [Amount] )").candidates
    assert [(c.kind, c.name) for c in found] == [
        ("bare_table", "Sales"),
        ("bare_bracket", "Amount"),
    ]


def test_function_names_are_not_tables():
    extraction = extract("SUM ( Sales[Quantity] )")
    assert "SUM" in extraction.functions
    assert extraction.of_kind("bare_table") == ()


def test_bare_table_reference():
    assert [c.name for c in extract("ALL ( Sales )").of_kind("bare_table")] == ["Sales"]


def test_strings_are_not_references():
    assert extract('"[Not A Column]" & "Sales[Nope]"').candidates == ()


def test_comment_references_are_not_extracted():
    expression = "Sales[Real] // Sales[Commented]\n-- Sales[AlsoCommented]"
    assert [str(c) for c in extract(expression).candidates] == ["Sales[Real]"]


# -- variables ------------------------------------------------------------------------------


def test_var_names_are_not_table_references():
    expression = """
    VAR Revenue = [Total Sales]
    VAR Cost    = [Total Cost]
    RETURN
        Revenue - Cost
    """
    extraction = extract(expression)
    assert extraction.variables == {"revenue", "cost"}
    assert extraction.of_kind("bare_table") == ()
    assert [c.name for c in extraction.of_kind("bare_bracket")] == ["Total Sales", "Total Cost"]


def test_variable_shadowing_a_table_name_is_still_a_variable():
    extraction = extract("VAR Sales = 1 RETURN Sales + 1")
    assert extraction.variables == {"sales"}
    assert extraction.of_kind("bare_table") == ()


def test_keywords_are_never_references():
    extraction = extract("EVALUATE Sales ORDER BY Sales[Amount] DESC")
    assert [str(c) for c in extraction.candidates] == ["Sales", "Sales[Amount]"]


# -- realistic shapes -------------------------------------------------------------------------


def test_calculate_with_a_filter_argument():
    extraction = extract('CALCULATE ( [Total Sales], Product[Color] = "Red" )')
    assert [str(c) for c in extraction.candidates] == ["[Total Sales]", "Product[Color]"]
    assert "CALCULATE" in extraction.functions


def test_nameof_inside_a_field_parameter():
    """Field parameters are the classic source of a false 'unused' verdict."""
    expression = (
        '{ ( "Revenue", NAMEOF ( \'Sales\'[Amount] ), 0 ), '
        '( "Units", NAMEOF ( Sales[Quantity] ), 1 ) }'
    )
    assert [str(c) for c in extract(expression).candidates] == [
        "Sales[Amount]",
        "Sales[Quantity]",
    ]


def test_userelationship_endpoints():
    expression = "CALCULATE ( [Sales], USERELATIONSHIP ( Sales[ShipDate], 'Date'[Date] ) )"
    assert [str(c) for c in extract(expression).candidates] == [
        "[Sales]",
        "Sales[ShipDate]",
        "Date[Date]",
    ]


def test_empty_and_blank_expressions():
    assert extract(None).candidates == ()
    assert extract("").candidates == ()
    assert extract("   \n  ").candidates == ()


def test_malformed_input_is_reported_not_raised():
    extraction = extract('Sales[Amount] & "unterminated')
    assert extraction.malformed
    assert [str(c) for c in extraction.candidates] == ["Sales[Amount]"]
