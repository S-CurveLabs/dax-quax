"""TMDL reader and .pbip loader tests.

The important one is at the bottom: the same logical model loaded live and from TMDL must
produce an identical lineage graph. That equivalence is what keeps the two sources honest,
because nothing else can catch one of them quietly losing an edge.
"""

from __future__ import annotations

import json

import pytest

from dax_quax.errors import DaxQuaxError
from dax_quax.sources.pbip import discover, load_model, open_pbip
from dax_quax.sources.tmdl import parse_tmdl, split_reference, unquote

PBIP = "tests/fixtures/synthetic_pbip"


# -- the TMDL reader ---------------------------------------------------------------------------


def test_blocks_nest_by_indent():
    document = parse_tmdl("table Sales\n\tcolumn Quantity\n\t\tdataType: int64\n")
    table = document.root
    assert (table.keyword, table.name) == ("table", "Sales")
    column = table.first("column")
    assert column.name == "Quantity"
    assert column.prop("dataType") == "int64"


def test_bare_property_is_a_true_flag():
    document = parse_tmdl("table Sales\n\tcolumn Quantity\n\t\tisHidden\n")
    assert document.root.first("column").flag("isHidden")
    assert not document.root.first("column").flag("isKey")


def test_expression_properties_use_equals_not_colon():
    """A partition's `source =` is a property, not a block. Missing this form leaves
    whole M queries reported as unparsed noise."""
    text = "table Sales\n\tpartition Sales = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n"
    partition = parse_tmdl(text).root.first("partition")
    assert partition.value == "m"
    assert partition.prop("mode") == "import"
    assert partition.prop("source") == "let x = 1 in x"


def test_quoted_names_and_escapes():
    document = parse_tmdl("table 'Sales Header'\n\tcolumn 'It''s Odd'\n")
    assert document.root.name == "Sales Header"
    assert document.root.first("column").name == "It's Odd"


def test_inline_expression():
    document = parse_tmdl("table Sales\n\tmeasure Total = SUM ( Sales[Quantity] )\n")
    assert document.root.first("measure").value == "SUM ( Sales[Quantity] )"


def test_multiline_expression_runs_deeper_than_the_properties():
    text = (
        "table Sales\n"
        "\tmeasure Total =\n"
        "\t\t\tVAR x = 1\n"
        "\t\t\tRETURN x\n"
        "\t\tformatString: 0\n"
    )
    measure = parse_tmdl(text).root.first("measure")
    assert measure.value == "VAR x = 1\nRETURN x"
    assert measure.prop("formatString") == "0"


def test_fenced_expression():
    text = "table Sales\n\tmeasure Total = ```\n\t\t\tSUM ( Sales[Q] )\n\t\t\t```\n"
    assert parse_tmdl(text).root.first("measure").value == "SUM ( Sales[Q] )"


def test_description_comments_attach_to_the_next_block():
    text = "table Sales\n\t/// How much was sold.\n\tmeasure Total = 1\n"
    assert parse_tmdl(text).root.first("measure").description == "How much was sold."


def test_unrecognised_lines_are_recorded_not_dropped():
    document = parse_tmdl("table Sales\n\t!!! nonsense here\n")
    assert document.unparsed
    assert "nonsense" in document.unparsed[0]


def test_parser_never_raises_on_rubbish():
    assert parse_tmdl("\x00\x01 ][ ''' = = =").unparsed


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sales.Quantity", ("Sales", "Quantity")),
        ("Sales.'Product Key'", ("Sales", "Product Key")),
        ("'Sales Header'.Amount", ("Sales Header", "Amount")),
        ("'A''B'.'C''D'", ("A'B", "C'D")),
        ("Quantity", (None, "Quantity")),
    ],
)
def test_split_reference(text, expected):
    assert split_reference(text) == expected


def test_unquote():
    assert unquote("'Sort Order'") == "Sort Order"
    assert unquote("SortOrder") == "SortOrder"


# -- discovery ------------------------------------------------------------------------------------


def test_discover_finds_every_artefact():
    project = discover(PBIP)
    assert project.name == "Contoso"
    assert project.semantic_model.name == "Contoso.SemanticModel"
    assert [r.name for r in project.reports] == ["Contoso.Report"]
    assert project.pbip_file.name == "Contoso.pbip"
    assert project.cache_abf is None


def test_discover_accepts_the_pbip_file_or_the_model_folder():
    assert discover(f"{PBIP}/Contoso.pbip").semantic_model is not None
    assert discover(f"{PBIP}/Contoso.SemanticModel").semantic_model is not None


def test_discover_rejects_something_that_is_not_a_project(tmp_path):
    with pytest.raises(DaxQuaxError, match="not a .pbip project"):
        discover(tmp_path / "nope.txt")


# -- loading --------------------------------------------------------------------------------------


def test_model_shape(pbip_model):
    assert set(pbip_model.tables) == {
        "Sales",
        "Product",
        "LocalDateTable_8f3c1e2a-0000-4000-9000-1234567890ab",
    }
    assert len(pbip_model.columns) == 8
    assert len(pbip_model.measures) == 3


def test_calculated_column_detected_from_its_expression(pbip_model):
    line_amount = pbip_model.columns["Sales[Line Amount]"]
    assert line_amount.is_calculated
    assert "RELATED" in line_amount.expression
    assert not pbip_model.columns["Sales[Quantity]"].is_calculated


def test_sort_by_resolves_to_a_key(pbip_model):
    assert pbip_model.columns["Product[Product Name]"].sort_by == "Product[Sort Order]"


def test_metadata_details_survive(pbip_model):
    assert pbip_model.columns["Product[List Price]"].data_type == "Decimal"
    assert pbip_model.columns["Product[Product Name]"].display_folder == "Attributes"
    assert pbip_model.columns["Sales[Product Key]"].is_hidden
    assert pbip_model.measures["[Margin % (old)]"].is_hidden
    assert pbip_model.tables["Sales"].mode == "import"


def test_descriptions_are_captured(pbip_model):
    assert "Superseded" in pbip_model.measures["[Margin % (old)]"].description


def test_relationship_endpoints(pbip_model):
    assert len(pbip_model.relationships) == 1
    rel = pbip_model.relationships[0]
    assert (rel.from_table, rel.from_column) == ("Sales", "Product Key")
    assert (rel.to_table, rel.to_column) == ("Product", "Product Key")


def test_the_whole_fixture_parses_cleanly(pbip_model):
    """Any 'not understood' warning means the reader is losing metadata."""
    assert not any("not understood" in w for w in pbip_model.warnings)


def test_multiline_measure_expression_survives(pbip_model):
    expression = pbip_model.measures["[Margin % (old)]"].expression
    assert expression == "DIVIDE ( [Total Sales], 1 )"


# -- capabilities-------------------------------------------------------------------------------


def test_pbip_has_no_metrics_and_says_why(pbip_model):
    assert not pbip_model.has("metrics")
    assert any("cache.abf" in w for w in pbip_model.warnings)


def test_open_pbip_attaches_the_sibling_report():
    model = open_pbip(PBIP)
    assert model.has("report")
    assert [r.name for r in model.reports] == ["Contoso.Report"]


def test_open_pbip_can_skip_the_reports():
    model = open_pbip(PBIP, attach_reports=False)
    assert not model.has("report")
    assert model.reports == []


def test_a_project_with_no_report_warns(tmp_path):
    import shutil

    target = tmp_path / "Lonely"
    shutil.copytree(f"{PBIP}/Contoso.SemanticModel", target / "Lonely.SemanticModel")
    model = open_pbip(target)
    assert any("no .Report folder" in w for w in model.warnings)
    assert not model.has("report")


def test_cache_without_pbixray_warns_and_grants_no_metrics(tmp_path):
    """A cache we cannot read must not silently look like a model with no data."""
    import shutil

    target = tmp_path / "Cached"
    shutil.copytree(f"{PBIP}/Contoso.SemanticModel", target / "Cached.SemanticModel")
    cache = target / "Cached.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"not really an abf")
    model = load_model(target)
    assert not model.has("metrics")
    assert any("pbixray" in w or "could not read" in w for w in model.warnings)


# -- TMSL fallback, for projects saved before TMDL -------------------------------------------------


def test_model_bim_is_read_when_there_is_no_tmdl(tmp_path):
    project = tmp_path / "Old"
    model_dir = project / "Old.Dataset"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bim").write_text(
        json.dumps(
            {
                "model": {
                    "tables": [
                        {
                            "name": "Sales",
                            "columns": [
                                {"name": "Amount", "dataType": 8},
                                {
                                    "name": "Calc",
                                    "type": "calculated",
                                    "expression": ["Sales[Amount]", " * 2"],
                                },
                            ],
                            "measures": [
                                {"name": "Total", "expression": "SUM ( Sales[Amount] )"}
                            ],
                        }
                    ],
                    "relationships": [],
                }
            }
        ),
        encoding="utf-8",
    )
    model = load_model(project)
    assert model.columns["Sales[Amount]"].data_type == "Double"
    assert model.columns["Sales[Calc]"].is_calculated
    assert model.columns["Sales[Calc]"].expression == "Sales[Amount]\n * 2"
    assert model.measures["[Total]"].expression == "SUM ( Sales[Amount] )"


def test_a_model_folder_with_neither_format_is_an_error(tmp_path):
    project = tmp_path / "Empty"
    (project / "Empty.SemanticModel").mkdir(parents=True)
    with pytest.raises(DaxQuaxError, match="neither definition/"):
        load_model(project)


# -- M3 ACCEPTANCE------------------------------------------------------------------------------


def test_live_and_pbip_produce_identical_lineage(synthetic_model, pbip_model):
    """The milestone's acceptance criterion.

    The two sources share only the DAX layer, so an edge present in one and missing from
    the other means a loader is dropping metadata. Nothing else catches that.
    """
    live = synthetic_model.lineage()
    pbip = pbip_model.lineage()
    assert pbip.edge_pairs() == live.edge_pairs()


def test_live_and_pbip_have_the_same_objects(synthetic_model, pbip_model):
    assert set(pbip_model.tables) == set(synthetic_model.tables)
    assert set(pbip_model.measures) == set(synthetic_model.measures)
    # The live model carries the engine's internal row-number column; TMDL does not
    # express one, and it is excluded from the graph either way.
    live_columns = {k for k, c in synthetic_model.columns.items() if not c.is_row_number}
    assert set(pbip_model.columns) == live_columns


def test_live_and_pbip_agree_on_what_is_referenced(synthetic_model, pbip_model):
    live = synthetic_model.lineage()
    pbip = pbip_model.lineage()
    for key in pbip_model.columns:
        assert pbip.is_referenced(key) == live.is_referenced(key), key


def test_pbip_produces_verdicts_without_any_sizes():
    """No cache.abf means no bytes — but the usage half of the answer still works."""
    from dax_quax.analysis.usage import Verdict, assess

    findings = {f.key: f for f in assess(open_pbip(PBIP))}
    assert findings["Sales[Order Number]"].verdict is Verdict.REMOVE
    assert findings["Sales[Order Number]"].bytes is None
    # kept only because a card colours itself by it
    assert findings["[Margin % (old)]"].verdict is Verdict.KEEP


# -- calculated tables (code review, section C) --------------------------------------------


def test_a_calculated_table_is_distinguishable_from_an_imported_one(pbip_model):
    """A calculated partition reads `= calculated` but its mode is still `import`, so
    mode cannot be used to tell DAX source from M source."""
    auto = pbip_model.tables["LocalDateTable_8f3c1e2a-0000-4000-9000-1234567890ab"]
    assert auto.is_calculated
    assert auto.mode == "import"
    assert not pbip_model.tables["Sales"].is_calculated
    assert pbip_model.tables["Sales"].source.startswith("let")
