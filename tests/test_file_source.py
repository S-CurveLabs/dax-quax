"""The .pbix / .abf source.

No binary file is involved. `build_model_from_source` takes the pbixray object rather than
a path, so a stand-in carrying the same frames exercises the whole transformation — which
is the same split that makes `dmv.build_model` testable without a live connection.

What these tests cannot prove is that a real pbixray emits these column names. That is what
`verify_contract` is for: one run against an actual .pbix settles every alias at once.
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from dax_quax.errors import DaxQuaxError
from dax_quax.sources.file import (
    FRAMES,
    build_model_from_source,
    load_file,
    verify_contract,
)


class FakeSource:
    """Stands in for a PBIXRay instance, carrying whichever frames a test needs."""

    def __init__(self, **frames: pd.DataFrame) -> None:
        self._frames = frames
        self.closed = False

    def __getattr__(self, name: str) -> object:
        try:
            return self._frames[name]
        except KeyError:
            raise AttributeError(name) from None

    def close(self) -> None:
        self.closed = True


def a_model(**overrides) -> FakeSource:
    frames = {
        "schema": pd.DataFrame(
            [
                {"TableName": "Sales", "ColumnName": "Amount", "PandasDataType": "float64"},
                {"TableName": "Sales", "ColumnName": "Order No", "PandasDataType": "object"},
                {"TableName": "Sales", "ColumnName": "Margin", "PandasDataType": "float64"},
                {"TableName": "Product", "ColumnName": "Name", "PandasDataType": "object"},
            ]
        ),
        "statistics": pd.DataFrame(
            [
                {"TableName": "Sales", "ColumnName": "Amount", "Cardinality": 900,
                 "Dictionary": 2048, "DataSize": 512, "HierarchiesSize": 64,
                 "Encoding": "VALUE", "RowCount": 1000},
                {"TableName": "Sales", "ColumnName": "Order No", "Cardinality": 1000,
                 "Dictionary": 65536, "DataSize": 4096, "HierarchiesSize": 0,
                 "Encoding": "HASH", "RowCount": 1000},
            ]
        ),
        "dax_measures": pd.DataFrame(
            [{"TableName": "Sales", "Name": "Total", "Expression": "SUM ( Sales[Amount] )",
              "DisplayFolder": "Core", "Description": "the total"}]
        ),
        "dax_columns": pd.DataFrame(
            [{"TableName": "Sales", "ColumnName": "Margin",
              "Expression": "Sales[Amount] * 0.3"}]
        ),
        "dax_tables": pd.DataFrame([]),
        "relationships": pd.DataFrame(
            [{"FromTableName": "Sales", "FromColumnName": "Amount",
              "ToTableName": "Product", "ToColumnName": "Name",
              "IsActive": True, "CrossFilteringBehavior": "OneDirection"}]
        ),
        "rls": pd.DataFrame(
            [{"RoleName": "Rep", "TableName": "Product",
              "FilterExpression": "Product[Name] = USERNAME ()"}]
        ),
    }
    frames.update(overrides)
    return FakeSource(**frames)


# -- the contract ------------------------------------------------------------------------


def test_contract_resolves_against_the_expected_names():
    report = verify_contract(a_model())
    assert report.clean
    assert report.resolved["schema"]["table"] == "TableName"
    assert report.resolved["statistics"]["dictionary"] == "Dictionary"


def test_an_alias_is_accepted():
    """The frames' exact column names are unverified, so each field lists candidates."""
    source = a_model(
        schema=pd.DataFrame([{"Table": "Sales", "Column": "Amount", "DataType": "float64"}])
    )
    report = verify_contract(source)
    assert report.resolved["schema"]["table"] == "Table"
    assert report.clean


def test_an_unresolvable_field_is_named_not_guessed():
    source = a_model(
        statistics=pd.DataFrame([{"TableName": "Sales", "ColumnName": "Amount", "Wat": 1}])
    )
    report = verify_contract(source)
    assert not report.clean
    assert any("statistics.dictionary" in m for m in report.missing)
    assert any("tried Dictionary, DictionarySize" in m for m in report.missing)


def test_a_missing_optional_frame_is_not_an_error():
    source = FakeSource(schema=a_model().schema)
    assert verify_contract(source).absent_frames == []


def test_a_missing_required_frame_is_reported():
    assert verify_contract(FakeSource()).absent_frames == ["schema"]


# -- building ------------------------------------------------------------------------------


@pytest.fixture
def model():
    return build_model_from_source(a_model(), name="Contoso")


def test_tables_and_columns(model):
    assert set(model.tables) == {"Sales", "Product"}
    assert "Sales[Order No]" in model.columns
    assert model.columns["Sales[Amount]"].data_type == "float64"


def test_measures(model):
    measure = model.measures["[Total]"]
    assert measure.table == "Sales"
    assert measure.expression == "SUM ( Sales[Amount] )"
    assert measure.display_folder == "Core"


def test_a_calculated_column_is_marked_from_its_expression(model):
    margin = model.columns["Sales[Margin]"]
    assert margin.is_calculated
    assert margin.expression == "Sales[Amount] * 0.3"
    assert not model.columns["Sales[Amount]"].is_calculated


def test_metrics(model):
    metrics = model.columns["Sales[Order No]"].metrics
    assert metrics.dictionary_bytes == 65536
    assert metrics.data_bytes == 4096
    assert metrics.total_bytes == 69632
    assert metrics.cardinality == 1000
    assert metrics.encoding == "HASH"


def test_a_column_with_no_statistics_row_stays_unmeasured(model):
    """Not measured and empty are different facts, on this source as on every other."""
    assert model.columns["Product[Name]"].metrics is None
    assert model.columns["Sales[Amount]"].metrics is not None


def test_relationships(model):
    relationship = model.relationships[0]
    assert (relationship.from_table, relationship.to_table) == ("Sales", "Product")
    assert relationship.is_active


def test_roles_and_filters(model):
    role = model.roles[0]
    assert role.name == "Rep"
    assert role.table_permissions[0].table == "Product"
    assert "USERNAME" in role.table_permissions[0].filter_expression


def test_capabilities_are_metadata_expressions_and_metrics(model):
    """The only source that has metrics and expressions without Desktop or a cache."""
    assert model.has("metrics")
    assert model.has("expressions")
    assert not model.has("report")


def test_the_model_works_end_to_end(model):
    lineage = model.lineage()
    assert "[Total]" in lineage.dependents("Sales[Amount]")
    assert lineage.is_referenced("Product[Name]")  # relationship endpoint


# -- when the file is not what was hoped ----------------------------------------------------------


def test_a_file_with_no_model_says_what_that_probably_means():
    with pytest.raises(DaxQuaxError, match="no embedded model"):
        build_model_from_source(FakeSource(), name="Thin")


def test_no_statistics_leaves_no_metrics_and_says_why():
    model = build_model_from_source(a_model(statistics=pd.DataFrame([])), name="x")
    assert not model.has("metrics")
    assert any("no storage statistics" in w for w in model.warnings)


def test_statistics_that_match_nothing_are_reported():
    """Two frames disagreeing about naming would otherwise read as a model with no data."""
    source = a_model(
        statistics=pd.DataFrame(
            [{"TableName": "OTHER", "ColumnName": "Amount", "Dictionary": 10, "DataSize": 1}]
        )
    )
    model = build_model_from_source(source, name="x")
    assert any("none matched a column of the schema" in w for w in model.warnings)


def test_unresolved_fields_surface_as_a_warning():
    source = a_model(
        dax_measures=pd.DataFrame([{"TableName": "Sales", "Name": "Total", "Wat": 1}])
    )
    model = build_model_from_source(source, name="x")
    assert any("did not resolve" in w for w in model.warnings)


def test_a_relationship_with_unreadable_endpoints_is_reported():
    source = a_model(relationships=pd.DataFrame([{"FromTableName": "Sales"}]))
    model = build_model_from_source(source, name="x")
    assert any("unreadable endpoints" in w for w in model.warnings)


# -- the entry point----------------------------------------------------------------------------


def test_load_file_rejects_a_directory(tmp_path):
    with pytest.raises(DaxQuaxError, match="not a file"):
        load_file(tmp_path)


def test_load_file_rejects_the_wrong_extension(tmp_path):
    path = tmp_path / "model.pbip"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(DaxQuaxError, match="a folder, not a file"):
        load_file(path)


def test_open_file_is_wired_up():
    import dax_quax

    assert callable(dax_quax.open_file)


#: Fields of FRAMES that no real .pbix has, verified 2026-09-19 against four Microsoft
#: samples. pbixray's `statistics` frame carries TableName, ColumnName, Cardinality,
#: Dictionary, HashIndex, DataSize and two timestamps -- and nothing else. So these three
#: resolve to nothing on every file, and the metadata they would have filled stays None
#: rather than becoming a zero. The aliases are kept: they cost a dictionary lookup and
#: they are what a future pbixray would most plausibly call these if it grew them.
PBIXRAY_HAS_NO = frozenset(
    {"statistics.hierarchy", "statistics.encoding", "statistics.rows"}
)


def _real_pbix_files():
    """The fetched corpus, plus DAXQUAX_TEST_PBIX if it is set."""
    import os

    found = sorted(pathlib.Path("tests/fixtures/pbix").glob("*.pbix"))
    extra = os.environ.get("DAXQUAX_TEST_PBIX")
    if extra:
        found.append(pathlib.Path(extra))
    return found


@pytest.mark.pbix
def test_against_real_pbix_files():
    """Settles every alias in FRAMES against real files.

    One file proves less than it looks: a model with no calculated columns says nothing
    about the dax_columns frame. Hence a spread -- run `python tools/fetch_pbix.py`.
    """
    from pbixray import PBIXRay

    files = _real_pbix_files()
    if not files:
        pytest.skip("run `python tools/fetch_pbix.py`, or set DAXQUAX_TEST_PBIX")

    for path in files:
        report = verify_contract(PBIXRay(str(path)))
        assert report.absent_frames == [], f"{path.name}: no {report.absent_frames}"
        unexpected = [m for m in report.missing if m.split(" (")[0] not in PBIXRAY_HAS_NO]
        assert unexpected == [], f"{path.name}: pbixray contract is wrong: {unexpected}"


@pytest.mark.pbix
def test_the_fields_pbixray_lacks_are_still_the_same_three():
    """If pbixray grows one of these, this fails and PBIXRAY_HAS_NO should shrink."""
    from pbixray import PBIXRay

    files = _real_pbix_files()
    if not files:
        pytest.skip("run `python tools/fetch_pbix.py`, or set DAXQUAX_TEST_PBIX")

    still_absent = set(PBIXRAY_HAS_NO)
    for path in files:
        report = verify_contract(PBIXRay(str(path)))
        still_absent &= {m.split(" (")[0] for m in report.missing}
    assert still_absent == PBIXRAY_HAS_NO, (
        f"pbixray now provides {sorted(PBIXRAY_HAS_NO - still_absent)}; "
        "remove it from PBIXRAY_HAS_NO and the model will start carrying it"
    )


@pytest.mark.pbix
def test_a_real_pbix_loads_into_a_model():
    """The contract resolving is not the same as the file producing a usable model."""
    from dax_quax.sources.file import load_file

    files = _real_pbix_files()
    if not files:
        pytest.skip("run `python tools/fetch_pbix.py`, or set DAXQUAX_TEST_PBIX")

    for path in files:
        model = load_file(path)
        assert model.tables, f"{path.name}: no tables"
        assert model.columns, f"{path.name}: no columns"
        assert model.lineage().unresolved == () or model.measures, path.name


def test_every_frame_this_module_reads_exists_on_pbixray():
    """Half the contract can be checked with no .pbix at all.

    Which *frames* exist is a property of the installed class; only their column names
    need a real file. Skipped when the `file` extra is not installed.
    """
    pbixray = pytest.importorskip("pbixray")

    surface = set(dir(pbixray.PBIXRay))
    missing = sorted(name for name in FRAMES if name not in surface)
    assert missing == [], f"pbixray has no {missing}; FRAMES is out of date"


def test_a_file_with_no_dax_at_all_says_so():
    """pbixray returns nothing for dax_measures on some real .pbix files.

    Microsoft's own "Adventure Works DW 2020" sample is one: it has plenty of measures and
    pbixray reports zero. A model that claims the `expressions` capability while holding no
    expressions is the same lie as claiming `metrics` with every size 0 -- nothing
    references anything, so every column looks unused. The scope degrade saves it while a
    .pbix has no report layer, but `--pbix --report-folder` removes that protection.
    """
    from dax_quax.sources.file import build_model_from_source

    source = a_model(
        dax_measures=pd.DataFrame(columns=["TableName", "Name", "Expression"]),
        dax_columns=pd.DataFrame(columns=["TableName", "ColumnName", "Expression"]),
        dax_tables=pd.DataFrame(columns=["TableName", "Expression"]),
    )
    model = build_model_from_source(source, name="silent")
    assert model.tables
    assert not model.measures
    assert any("no DAX was read" in w for w in model.warnings)


def test_a_file_with_dax_does_not_carry_that_warning():
    from dax_quax.sources.file import build_model_from_source

    model = build_model_from_source(a_model(), name="fine")
    assert not any("no DAX was read" in w for w in model.warnings)
