"""R2: four more ways a live object used to be reported as safe to delete.

Every case here is the same failure — something the model or the report genuinely uses,
reported as unreferenced — reached by a different route:

    a legacy report.json    the report was counted as scanned but read nothing
    formatStringDefinition  a measure's dynamic format string was never walked
    groupByColumn           a field parameter's hidden column is named nowhere else
    model.bim               the TMSL loader read no roles and no hierarchies

Each builds the smallest project that shows it, because the shared fixtures are PBIR and
TMDL and none of these can be expressed there.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from dax_quax.analysis.usage import Verdict, assess, scope_of
from dax_quax.report import load_report
from dax_quax.sources.pbip import open_pbip

MODEL_TMDL = """\
table Sales

\tcolumn Amount
\t\tdataType: decimal
\t\tsourceColumn: Amount

\tcolumn Region
\t\tdataType: string
\t\tsourceColumn: Region

\tpartition Sales = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet Source = Sql.Database("s", "d") in Source
"""


def _project(root: pathlib.Path, *, tables: dict[str, str] | None = None) -> pathlib.Path:
    """A .pbip project with one semantic model and no report."""
    definition = root / "Contoso.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True)
    (root / "Contoso.SemanticModel" / "definition.pbism").write_text(
        '{"version": "4.2", "settings": {}}', encoding="utf-8"
    )
    (definition / "model.tmdl").write_text("model Model\n\tculture: en-US\n", encoding="utf-8")
    for name, text in (tables or {"Sales": MODEL_TMDL}).items():
        (definition / "tables" / f"{name}.tmdl").write_text(text, encoding="utf-8")
    (root / "Contoso.pbip").write_text("{}", encoding="utf-8")
    return root


def _report(root: pathlib.Path, *, legacy: bool) -> None:
    report = root / "Contoso.Report"
    (report / ("" if legacy else "definition")).mkdir(parents=True, exist_ok=True)
    (report / "definition.pbir").write_text(
        '{"version": "1.0", "datasetReference": {"byPath": '
        '{"path": "../Contoso.SemanticModel"}}}',
        encoding="utf-8",
    )
    if legacy:
        (report / "report.json").write_text('{"sections": []}', encoding="utf-8")


# -- a report that was found but could not be read ------------------------------------------


@pytest.fixture
def legacy_project(tmp_path):
    _project(tmp_path)
    _report(tmp_path, legacy=True)
    return tmp_path


def test_a_legacy_report_is_not_a_readable_report(legacy_project):
    report = load_report(legacy_project / "Contoso.Report")
    assert report.readable is False
    assert report.bindings == []


def test_an_unreadable_report_does_not_count_as_scanned(legacy_project):
    scope = scope_of(open_pbip(legacy_project))
    assert scope.reports_scanned == 0
    assert scope.reports_unreadable == 1
    assert scope.trustworthy is False


def test_a_legacy_report_never_licenses_remove(legacy_project):
    """The whole point. Every column this report binds is invisible, so nothing is REMOVE."""
    findings = assess(open_pbip(legacy_project))
    assert {f.verdict for f in findings} == {Verdict.UNKNOWN}
    assert all(f.action is None for f in findings)
    assert any("could not be read" in spot for spot in findings[0].blind_spots)


def test_the_reason_why_says_the_report_could_not_be_read(legacy_project):
    finding = assess(open_pbip(legacy_project))[0]
    assert "could not be read at all" in finding.reason


def test_the_warning_reaches_the_model(legacy_project):
    model = open_pbip(legacy_project)
    assert any("legacy single-file report format" in w for w in model.warnings)


def test_a_report_folder_with_no_definition_is_also_unreadable(tmp_path):
    _project(tmp_path)
    _report(tmp_path, legacy=False)
    (tmp_path / "Contoso.Report" / "definition").rmdir()
    assert load_report(tmp_path / "Contoso.Report").readable is False


def test_an_empty_but_readable_report_still_licenses_remove(tmp_path):
    """The other half: a real PBIR report that binds nothing is evidence, not a blind spot."""
    _project(tmp_path)
    _report(tmp_path, legacy=False)
    model = open_pbip(tmp_path)
    assert scope_of(model).trustworthy is True
    assert {f.verdict for f in assess(model)} == {Verdict.REMOVE}


def test_the_workspace_scan_degrades_on_an_unreadable_report(tmp_path):
    from dax_quax.sources.workspace import discover_workspace

    _project(tmp_path / "project")
    _report(tmp_path / "project", legacy=True)
    scope = discover_workspace(tmp_path).scope_for("Contoso")
    assert (scope.reports_unreadable, scope.trustworthy) == (1, False)


# -- a measure's dynamic format string ----------------------------------------------------


FORMAT_TMDL = (
    MODEL_TMDL
    + """
\tmeasure 'Selected Currency' = "EUR"

\tmeasure 'Sales Amount' = SUM(Sales[Amount])
\t\tformatStringDefinition = IF([Selected Currency] = "EUR", "E#,##0", "$#,##0")
"""
)


@pytest.fixture
def format_model(tmp_path):
    _project(tmp_path, tables={"Sales": FORMAT_TMDL})
    _report(tmp_path, legacy=False)
    return open_pbip(tmp_path)


def test_a_dynamic_format_string_is_loaded(format_model):
    measure = format_model.measures["[Sales Amount]"]
    assert measure.format_expression is not None
    assert "Selected Currency" in measure.format_expression


def test_a_measure_used_only_by_a_dynamic_format_string_is_not_removable(format_model):
    findings = {f.key: f for f in assess(format_model)}
    assert findings["[Selected Currency]"].verdict is Verdict.KEEP


# -- a field parameter's grouping column ---------------------------------------------------


PARAMETER_TMDL = """\
table Parameter

\tcolumn Parameter
\t\tdataType: string
\t\tsourceColumn: [Value1]
\t\trelatedColumnDetails
\t\t\tgroupByColumn: 'Parameter Fields'

\tcolumn 'Parameter Fields'
\t\tdataType: string
\t\tisHidden
\t\tsourceColumn: [Value2]

\tpartition Parameter = calculated
\t\tmode: import
\t\tsource = {("Amount", NAMEOF('Sales'[Amount]), 0)}
"""


@pytest.fixture
def parameter_model(tmp_path):
    _project(tmp_path, tables={"Sales": MODEL_TMDL, "Parameter": PARAMETER_TMDL})
    _report(tmp_path, legacy=False)
    return open_pbip(tmp_path)


def test_a_group_by_column_resolves_to_a_model_key(parameter_model):
    column = parameter_model.columns["Parameter[Parameter]"]
    assert column.group_by == "Parameter[Parameter Fields]"


def test_the_grouping_column_is_kept_alive_by_the_column_that_groups_by_it(parameter_model):
    finding = {f.key: f for f in assess(parameter_model)}["Parameter[Parameter Fields]"]
    assert finding.verdict is not Verdict.REMOVE
    assert finding.model_refs >= 1


def test_a_group_by_naming_a_missing_column_is_reported_not_guessed(tmp_path):
    broken = PARAMETER_TMDL.replace("groupByColumn: 'Parameter Fields'",
                                    "groupByColumn: 'Nothing At All'")
    _project(tmp_path, tables={"Sales": MODEL_TMDL, "Parameter": broken})
    model = open_pbip(tmp_path)
    assert model.columns["Parameter[Parameter]"].group_by is None
    assert any("groups by" in warning for warning in model.warnings)


# -- model.bim, the pre-TMDL shape ----------------------------------------------------------


TMSL = {
    "model": {
        "tables": [
            {
                "name": "Customer",
                "columns": [
                    {"name": "Region", "dataType": "string"},
                    {"name": "Country", "dataType": "string"},
                    {"name": "Email", "dataType": "string"},
                ],
                "hierarchies": [
                    {"name": "Geography", "levels": [{"name": "Country", "column": "Country"}]}
                ],
                "measures": [
                    {
                        "name": "Total",
                        "expression": "COUNTROWS(Customer)",
                        "formatStringDefinition": {"expression": "[Symbol]"},
                    },
                    {"name": "Symbol", "expression": '"$"'},
                ],
                "partitions": [
                    {
                        "name": "p",
                        "mode": "import",
                        "source": {"type": "m", "expression": "let x = 1 in x"},
                    }
                ],
            }
        ],
        "roles": [
            {
                "name": "Sales Rep",
                "modelPermission": "read",
                "tablePermissions": [
                    {"table": "Customer", "filterExpression": 'Customer[Region] = "West"'}
                ],
            }
        ],
    }
}


@pytest.fixture
def tmsl_model(tmp_path):
    model_dir = tmp_path / "Contoso.SemanticModel"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bim").write_text(json.dumps(TMSL), encoding="utf-8")
    (model_dir / "definition.pbism").write_text('{"version": "4.2"}', encoding="utf-8")
    (tmp_path / "Contoso.pbip").write_text("{}", encoding="utf-8")
    _report(tmp_path, legacy=False)
    return open_pbip(tmp_path)


def test_tmsl_reads_roles_and_their_filters(tmsl_model):
    assert [role.name for role in tmsl_model.roles] == ["Sales Rep"]
    assert "West" in tmsl_model.roles[0].table_permissions[0].filter_expression


def test_tmsl_reads_hierarchies_and_their_levels(tmsl_model):
    assert [h.name for h in tmsl_model.hierarchies] == ["Geography"]
    assert tmsl_model.hierarchies[0].levels[0].column == "Country"


@pytest.mark.parametrize(
    ("key", "keeper"),
    [
        ("Customer[Region]", "a row-level-security filter"),
        ("Customer[Country]", "a hierarchy level"),
        ("[Symbol]", "a dynamic format string"),
    ],
)
def test_tmsl_objects_with_exactly_one_keeper_are_not_removable(tmsl_model, key, keeper):
    findings = {f.key: f for f in assess(tmsl_model)}
    assert findings[key].verdict is not Verdict.REMOVE, keeper


def test_tmsl_still_reports_a_genuinely_unused_column(tmsl_model):
    findings = {f.key: f for f in assess(tmsl_model)}
    assert findings["Customer[Email]"].verdict is Verdict.REMOVE
