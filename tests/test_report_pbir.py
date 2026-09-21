"""PBIR parsing tests.

The fixture deliberately hides a measure reference deep inside a visual's `objects` block,
where conditional formatting keeps it. A parser that reads known query paths finds every
other binding in this fixture and misses that one — and reports a live measure as safe to
delete. That case is `test_conditional_formatting_is_found`.
"""

from __future__ import annotations

import json

from dax_quax.report import load_report


def located(report, key: str) -> set[str]:
    return {b.location for b in report.for_key(key)}


# -- shape ---------------------------------------------------------------------------------


def test_report_loads(synthetic_report):
    assert synthetic_report.name == "Contoso.Report"
    assert synthetic_report.pages == ("Overview",)
    assert synthetic_report.visual_count == 2


def test_field_well_bindings(synthetic_report):
    assert "field-well" in located(synthetic_report, "Product[Product Name]")
    assert "field-well" in located(synthetic_report, "[Total Quantity]")
    assert "field-well" in located(synthetic_report, "[Total Sales]")


def test_binding_carries_its_visual_context(synthetic_report):
    binding = synthetic_report.for_key("Product[Product Name]")[0]
    assert binding.page == "Overview"
    assert binding.visual == "v1"
    assert binding.visual_type == "barChart"


# -- the places a path whitelist would not look -----------------------------------------------


def test_conditional_formatting_is_found(synthetic_report):
    """A measure used only to colour a card is in use.

    This binding lives under visual.objects.labels[0].properties.color.solid.color.expr
    .Conditional.Cases[0].Condition.Comparison.Left — nowhere near the query state.
    """
    assert located(synthetic_report, "[Margin % (old)]") == {"conditional-format"}


def test_page_level_filter_is_found(synthetic_report):
    assert "filter" in located(synthetic_report, "Product[List Price]")


def test_bookmark_state_is_found(synthetic_report):
    assert "bookmark" in located(synthetic_report, "Product[Sort Order]")


def test_sort_definition_is_found(synthetic_report):
    assert "sort" in located(synthetic_report, "[Total Quantity]")


def test_every_binding_is_accounted_for(synthetic_report):
    assert len(synthetic_report.bindings) == 7


# -- honesty about what could not be read --------------------------------------------------------


def test_source_alias_reference_is_recorded_as_unparsed(synthetic_report):
    """A SourceRef naming a From alias rather than an entity cannot be resolved alone."""
    assert len(synthetic_report.unparsed) == 1
    assert "report.json" in synthetic_report.unparsed[0]


def test_legacy_report_format_says_what_to_do(tmp_path):
    root = tmp_path / "Old.Report"
    root.mkdir()
    (root / "report.json").write_text(json.dumps({"sections": []}), encoding="utf-8")
    report = load_report(root)
    assert report.bindings == []
    assert "legacy single-file report format" in report.warnings[0]
    assert "PBIR" in report.warnings[0]


def test_missing_definition_folder_warns(tmp_path):
    root = tmp_path / "Empty.Report"
    root.mkdir()
    assert "no definition/ folder" in load_report(root).warnings[0]


def test_unreadable_json_warns_rather_than_raising(tmp_path):
    visual = tmp_path / "Broken.Report/definition/pages/P1/visuals/v1"
    visual.mkdir(parents=True)
    (visual / "visual.json").write_text("{ not json", encoding="utf-8")
    report = load_report(tmp_path / "Broken.Report")
    assert any("could not read visual.json" in w for w in report.warnings)


# -- entry points ----------------------------------------------------------------------------------


def test_can_be_pointed_at_the_containing_folder(synthetic_report):
    """load_report finds the .Report folder itself, or a parent holding one."""
    assert synthetic_report.visual_count == 2


def test_keys_are_the_model_key_shape(synthetic_report):
    keys = synthetic_report.keys()
    assert "product[product name]" in keys
    assert "[total quantity]" in keys
