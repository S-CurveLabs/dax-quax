"""The loaders against real Power BI Desktop output.

Run with `pytest -m demo`, after `python tools/fetch_demo.py`. Deselected by default
because the corpus is borrowed rather than vendored and CI has no network (CONVENTIONS
§10).

WHAT THIS IS FOR
----------------
A synthetic fixture encodes the same assumptions the parser does, so it agrees with the
parser by construction and can only catch a regression, never a wrong assumption. This
corpus is the opposite: nothing in it was written with these loaders in mind. The first
time it met them it found four declared contracts wrong, each one a silent loss of
lineage of exactly the kind §6 says must not happen:

  - `kpi` was not a known TMDL block keyword, so a KPI's status, trend and target
    expressions were dropped. A goal measure referenced only there reported REMOVE.
  - `_read_multiline` used a block's indentation floor for a property's value, so a
    property whose body sat exactly one level in came back empty.
  - `culture` was a guess; the keyword is `cultureInfo`. `perspectiveTable`,
    `perspectiveColumn`, `perspectiveMeasure` and `database` were simply missing, and
    `ref table Calendar` has a two-word name the block pattern could not match.
  - A report reference may name its table through an alias that only the enclosing
    `From` clause defines, and a date hierarchy may hang off a column through a
    `PropertyVariationSource`. Both were recorded as unreadable, which is a binding not
    counted.

So the assertions here are mostly "nothing was unreadable". That is the finding.
"""

from __future__ import annotations

import pathlib

import pytest

from dax_quax.report.pbir import _binding, _walk, load_report
from dax_quax.sources.pbip import open_pbip
from dax_quax.sources.tmdl import parse_tmdl
from dax_quax.sources.workspace import discover_workspace

pytestmark = pytest.mark.demo

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "demo" / "src"


@pytest.fixture(scope="module", autouse=True)
def _require_corpus():
    if not CORPUS.is_dir():
        pytest.skip("run `python tools/fetch_demo.py` first")


@pytest.fixture(scope="module")
def workspace():
    return discover_workspace(CORPUS)


@pytest.fixture(scope="module")
def model01():
    return open_pbip(CORPUS / "Model01.SemanticModel")


# -- the whole corpus loads --------------------------------------------------------------


def test_every_model_and_report_is_found(workspace):
    assert len(workspace.models) == 3
    assert len(workspace.links) == 5


def test_every_report_matches_its_model(workspace):
    """An unmatched report degrades every verdict, so this is load-bearing."""
    assert workspace.unmatched == []
    assert workspace.scope_for("Model01").trustworthy


def test_no_tmdl_line_in_any_definition_file_is_unreadable():
    """The parser's keyword list is a declared contract. This is what checks it."""
    bad = {}
    for file in sorted(CORPUS.rglob("*.tmdl")):
        if "TMDLScripts" in str(file):
            continue  # scripts are a different grammar, not model definitions
        document = parse_tmdl(file.read_text(encoding="utf-8"))
        if document.unparsed:
            bad[file.name] = document.unparsed[:3]
    assert bad == {}


def test_no_model_reports_a_parse_warning(workspace):
    for name, model in workspace.models.items():
        unparsed = [w for w in model.warnings if "not understood" in w]
        assert unparsed == [], f"{name}: {unparsed}"


# -- TMDL: what the corpus caught ----------------------------------------------------------


def test_a_kpi_block_is_read(model01):
    """`kpi` is written inside a table file, which the loader does read."""
    margin = model01.measures["[Margin]"]
    assert len(margin.kpi_expressions) == 3
    assert any("MarginTolerance" in e for e in margin.kpi_expressions)


def test_a_kpi_expression_reaches_the_lineage_graph(model01):
    """Capturing the DAX is pointless unless something walks it."""
    lineage = model01.lineage()
    assert "[Margin %]" in lineage.dependencies("[Margin]")


def test_a_multi_line_property_value_is_whole(model01):
    """The status expression is four lines in. A block's floor would stop at zero of them."""
    status = next(e for e in model01.measures["[Margin]"].kpi_expressions if "SWITCH" in e)
    assert "RETURN" in status
    assert status.count("\n") > 5


def test_a_culture_file_parses():
    culture = CORPUS / "Model01.SemanticModel" / "definition" / "cultures" / "en-US.tmdl"
    document = parse_tmdl(culture.read_text(encoding="utf-8"))
    assert len(document.nodes) == 1
    assert document.nodes[0].keyword == "cultureInfo"
    # The linguistic metadata is a JSON blob several thousand lines long, as one value.
    assert document.nodes[0].prop("linguisticMetadata").startswith("{")


def test_a_perspective_file_parses():
    perspective = CORPUS / "Model01.SemanticModel" / "definition" / "perspectives" / "Sales.tmdl"
    document = parse_tmdl(perspective.read_text(encoding="utf-8"))
    tables = document.nodes[0].find("perspectiveTable")
    assert tables
    assert tables[0].find("perspectiveMeasure")


def test_a_two_word_ref_parses():
    """`ref table Calendar` — the block pattern wants one name, and this has two."""
    model_file = CORPUS / "Model01.SemanticModel" / "definition" / "model.tmdl"
    document = parse_tmdl(model_file.read_text(encoding="utf-8"))
    refs = [n for n in document.nodes if n.keyword == "ref"]
    assert {r.value for r in refs} == {"table", "cultureInfo", "perspective", "role"}
    assert "Calendar" in {r.name for r in refs}


def test_crlf_files_parse_the_same(model01):
    """The corpus is CRLF throughout and the synthetic fixtures are LF."""
    assert model01.tables
    assert model01.measures


# -- PBIR: what the corpus caught ------------------------------------------------------------


def test_no_report_reference_is_unreadable():
    """Each of these was a report binding that silently did not count."""
    bad = []
    for report_dir in sorted(CORPUS.glob("*.Report")):
        report = load_report(report_dir)
        bad += [f"{report.name}: {u}" for u in report.unparsed]
    assert bad == []


def test_a_source_alias_resolves_to_its_table():
    """A filter written against `{"Source": "c"}` means Calendar, via the From clause."""
    report = load_report(CORPUS / "Report01.Report")
    assert any(b.entity == "Calendar" and b.property == "Date" for b in report.bindings)


def test_a_date_hierarchy_binds_the_column_underneath_it():
    """Orders[Shipped Date] -> Date Hierarchy -> Month keeps the column, not the level."""
    report = load_report(CORPUS / "Report04.Report")
    bound = {(b.entity, b.property, b.kind) for b in report.bindings}
    assert ("Orders", "Shipped Date", "column") in bound


def test_an_unresolvable_alias_is_still_reported_rather_than_guessed():
    """Guessing which table "c" meant is how a column in use is reported as removable."""
    orphan = {"Column": {"Expression": {"SourceRef": {"Source": "zz"}}, "Property": "X"}}
    reference, trail, scope = next(iter(_walk(orphan, ())))
    assert _binding(reference, trail, "other", None, None, None, False, scope) is None


# -- the verdicts ----------------------------------------------------------------------------


def test_no_verdict_rests_on_a_reference_nobody_could_read(workspace):
    """The audit that started this: every object a missed reference pointed at."""
    for name in workspace.models:
        for finding in workspace.findings_for(name):
            assert finding.verdict != "REMOVE" or finding.model_refs == 0


def test_the_only_unresolved_reference_is_one_the_model_itself_gets_wrong(workspace):
    """Reading the KPI made a real defect in the corpus visible.

    Model01's Margin KPI targets `[Margin % Overall]`, and no such measure exists. While
    the kpi block went unread this was invisible; now it is a reported unresolved
    reference, which is what it has been all along.
    """
    unresolved = {
        name: [u.text for u in model.lineage().unresolved]
        for name, model in workspace.models.items()
    }
    assert unresolved["Model01"] == ["[Margin % Overall]"]
    assert unresolved["Model02"] == []
    assert unresolved["Model03"] == []
