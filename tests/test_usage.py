"""Verdict tests.

The synthetic model has four objects nothing in the model references:

    Sales[Order Number]     genuinely unused
    Product[Product Name]   used by a visual
    [Total Quantity]        used by a visual
    [Margin % (old)]        used by conditional formatting only

Without the report layer all four look identical. That is the whole point of UNKNOWN.
"""

from __future__ import annotations

import pytest

from dax_quax.analysis.usage import (
    EXTERNAL_REPORTS_CAVEAT,
    ScanScope,
    Thresholds,
    Verdict,
    assess,
    findings_frame,
    scope_of,
    summary,
)
from dax_quax.report import Binding


def by_key(findings) -> dict:
    return {f.key: f for f in findings}


@pytest.fixture
def without_report(synthetic_model):
    return by_key(assess(synthetic_model))


@pytest.fixture
def with_report(model_with_report):
    return by_key(assess(model_with_report))


# -- the tiering ------------------------------------------------------------------------------


def test_unreferenced_is_unknown_when_no_report_was_scanned(without_report):
    """Not REMOVE. We did not look everywhere, so we do not get to say 'delete it'."""
    assert without_report["Sales[Order Number]"].verdict is Verdict.UNKNOWN
    assert without_report["[Margin % (old)]"].verdict is Verdict.UNKNOWN


def test_unknown_findings_offer_no_action(without_report):
    assert without_report["Sales[Order Number]"].action is None


def test_unreferenced_becomes_remove_once_the_report_is_scanned(with_report):
    finding = with_report["Sales[Order Number]"]
    assert finding.verdict is Verdict.REMOVE
    assert "no visual, filter, conditional format or bookmark" in finding.reason
    assert finding.action == "Remove it from the Power Query load."


def test_a_measure_used_only_by_conditional_formatting_is_kept(with_report):
    """The single most valuable test in this file.

    [Margin % (old)] has zero model references. A parser that only reads visual query
    state would call it REMOVE, and deleting it would break a card's colouring.
    """
    finding = with_report["[Margin % (old)]"]
    assert finding.verdict is Verdict.KEEP
    assert finding.model_refs == 0
    assert finding.report_bindings == 1


def test_report_only_usage_keeps_a_column(with_report):
    finding = with_report["Product[Product Name]"]
    assert finding.verdict is Verdict.KEEP
    assert finding.model_refs == 0
    assert finding.report_bindings >= 1


def test_model_only_usage_keeps_a_column(with_report):
    """Product[Sort Order] exists only to order another column, and is used by a bookmark."""
    finding = with_report["Product[Sort Order]"]
    assert finding.verdict is Verdict.KEEP
    assert finding.model_refs == 1


def test_the_only_genuinely_removable_column(with_report):
    removable = {k for k, f in with_report.items() if f.verdict is Verdict.REMOVE}
    assert removable == {"Sales[Order Number]"}


# -- what was checked, and what could not be ------------------------------------------------------


def test_checked_lists_the_report_surfaces_when_scanned(with_report, without_report):
    assert "conditional formats" in with_report["Sales[Order Number]"].checked
    assert "conditional formats" not in without_report["Sales[Order Number]"].checked
    assert "sortByColumn" in without_report["Sales[Order Number]"].checked


def test_external_reports_caveat_is_on_every_finding(with_report):
    assert all(EXTERNAL_REPORTS_CAVEAT in f.blind_spots for f in with_report.values())


def test_missing_report_layer_is_a_declared_blind_spot(without_report, with_report):
    assert "the report layer was not scanned" in without_report["Sales[Order Number]"].blind_spots
    assert "the report layer was not scanned" not in with_report["Sales[Order Number]"].blind_spots


def test_lineage_gaps_are_carried_into_blind_spots(with_report):
    joined = " ".join(with_report["Sales[Order Number]"].blind_spots)
    assert "perspectives" in joined
    assert "translations" in joined


# -- review rules ------------------------------------------------------------------------------


def test_a_large_calculated_column_is_reviewed(model_with_report):
    findings = by_key(assess(model_with_report, thresholds=Thresholds(large_bytes=400)))
    finding = findings["Sales[Line Amount]"]
    assert finding.verdict is Verdict.REVIEW
    assert "calculated column" in finding.reason
    assert "measure" in finding.action


def test_a_near_unique_key_is_reviewed(model_with_report):
    """Order Number is 1000 distinct over 1000 rows — but it is unused, so REMOVE wins.

    Cardinality is set on the column here rather than coming from the fixture: the DMV
    rowsets do not carry it (see test_cardinality_is_absent_rather_than_invented), so a
    live or replayed model cannot fire this rule at all. A .pbix can, because pbixray
    reports cardinality directly.
    """
    import dataclasses

    column = model_with_report.columns["Sales[Order Number]"]
    column.metrics = dataclasses.replace(column.metrics, cardinality=1000)
    model_with_report.reports[0].bindings.append(
        Binding(entity="Sales", property="Order Number", kind="column", location="field-well")
    )
    findings = by_key(assess(model_with_report, thresholds=Thresholds(large_bytes=400)))
    finding = findings["Sales[Order Number]"]
    assert finding.verdict is Verdict.REVIEW
    assert "near-unique key" in finding.reason


def test_thresholds_gate_the_rules(model_with_report):
    """With a high threshold nothing is expensive enough to question."""
    findings = by_key(assess(model_with_report, thresholds=Thresholds(large_bytes=10**9)))
    assert findings["Sales[Line Amount]"].verdict is Verdict.KEEP


def test_auto_date_time_columns_are_reviewed(synthetic_model, model_with_report):
    from dax_quax.model import Column, ColumnMetrics

    auto = next(t for t in model_with_report.tables.values() if t.is_auto_date_time)
    column = Column(table=auto.name, name="Date", metrics=ColumnMetrics(data_bytes=10))
    model_with_report.columns[column.key] = column
    # give it a reference so it is not simply unused
    model_with_report.reports[0].bindings.append(
        Binding(entity=auto.name, property="Date", kind="column", location="field-well")
    )
    finding = by_key(assess(model_with_report))[column.key]
    assert finding.verdict is Verdict.REVIEW
    assert "Auto date/time" in finding.action


# -- output ------------------------------------------------------------------------------------


def test_findings_are_sorted_by_size(with_report):
    sizes = [f.bytes or 0 for f in assess_list(with_report)]
    assert sizes == sorted(sizes, reverse=True)


def assess_list(mapping):
    return sorted(mapping.values(), key=lambda f: (-(f.bytes or 0), f.key))


def test_row_number_columns_are_not_findings(with_report):
    assert not any("RowNumber-" in key for key in with_report)


def test_summary_headline(model_with_report):
    findings = assess(model_with_report)
    head = summary(findings)
    assert head["removable_objects"] == 1
    assert head["removable_bytes"] == 7025
    assert head["measured_bytes"] == 8255  # every finding except the row-number column
    assert head["removable_pct"] == pytest.approx(7025 / 8255 * 100)


def test_frame_columns(with_report):
    frame = findings_frame(list(with_report.values()))
    assert "verdict" in frame.columns
    assert "report_bindings" in frame.columns
    assert set(frame["verdict"]) <= {"KEEP", "REVIEW", "REMOVE", "UNKNOWN"}


# -- scan scope ---------------------------------------------------------------------------------
#
# The workspace scan (CONVENTIONS section 14) lands after M3, but the safety rule it depends on
# is enforced now so that phase 2 only has to populate a number.


def test_scope_is_derived_from_the_model(model_with_report, synthetic_model):
    scope = scope_of(model_with_report)
    assert scope.reports_scanned == 1
    assert scope.report_names == ("Contoso.Report",)
    assert scope.trustworthy
    assert not scope_of(synthetic_model).any_report


def test_an_unmatched_report_downgrades_every_remove(model_with_report):
    """A report we could not tie to a model is usage we did not count.

    Missing a report causes a false REMOVE and someone deletes a live object. Missing a
    model is harmless. The two directions are not symmetric, so an unmatched report is
    treated exactly like having scanned nothing.
    """
    scope = ScanScope(model_source="pbip", reports_scanned=3, reports_unmatched=1)
    findings = by_key(assess(model_with_report, scope=scope))
    assert findings["Sales[Order Number]"].verdict is Verdict.UNKNOWN
    assert not any(f.verdict is Verdict.REMOVE for f in findings.values())


def test_the_downgrade_says_why(model_with_report):
    scope = ScanScope(model_source="pbip", reports_scanned=3, reports_unmatched=1)
    finding = by_key(assess(model_with_report, scope=scope))["Sales[Order Number]"]
    assert "could not be matched to a model" in finding.reason
    assert finding.action is None
    assert any("downgraded to UNKNOWN" in spot for spot in finding.blind_spots)


def test_scope_travels_on_every_finding(with_report):
    assert all(f.scan_scope.reports_scanned == 1 for f in with_report.values())


def test_scope_describes_itself():
    assert ScanScope().describe() == "no report scanned"
    assert ScanScope(reports_scanned=1).describe() == "1 report scanned"
    assert ScanScope(reports_scanned=4).describe() == "4 reports scanned"
    assert ScanScope(reports_scanned=4, reports_unmatched=1).describe() == (
        "4 reports scanned, 1 unmatched"
    )


# -- several reports over one model ----------------------------------------------------------------


def test_usage_is_unioned_across_reports(synthetic_rowsets, synthetic_report):
    """The reason phase 2 exists: one report's silence is not evidence."""
    import copy

    from dax_quax.report import ReportBindings, attach_report
    from dax_quax.sources import dmv

    model = dmv.build_model(synthetic_rowsets, name="Synthetic", source="live")
    second = ReportBindings(name="Exec.Report")
    second.bindings.append(
        Binding(entity="Sales", property="Order Number", kind="column", location="field-well")
    )
    attach_report(model, copy.deepcopy(synthetic_report), second)

    findings = by_key(assess(model))
    assert scope_of(model).reports_scanned == 2
    # Unused according to the first report, used by the second.
    assert findings["Sales[Order Number]"].verdict is not Verdict.REMOVE
    assert findings["Sales[Order Number]"].report_bindings == 1


def test_attach_report_with_nothing_grants_no_capability(synthetic_model):
    from dax_quax.report import attach_report

    attach_report(synthetic_model)
    assert not synthetic_model.has("report")
    assert synthetic_model.reports == []


def test_bindings_for_spans_every_report(model_with_report):
    from dax_quax.report import ReportBindings, attach_report, bindings_for

    extra = ReportBindings(name="Second.Report")
    extra.bindings.append(
        Binding(entity="Product", property="Product Name", kind="column", location="filter")
    )
    attach_report(model_with_report, extra)
    found = bindings_for(model_with_report, "Product[Product Name]")
    assert len(found) == 2
    assert {b.location for b in found} == {"field-well", "filter"}
