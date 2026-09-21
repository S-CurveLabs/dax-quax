"""Baseline, diff and CI gates.

The behaviour these tests care most about is what happens when a comparison is *not*
meaningful. A diff that reports verdict churn caused purely by scanning fewer reports, or a
gate that passes because it could not run, are both worse than no check at all — the second
especially, because a team comes to rely on a signal that is not there.
"""

from __future__ import annotations

import json

import pytest

from dax_quax.analysis.baseline import (
    Baseline,
    Gates,
    ObjectSnapshot,
    diff,
)
from dax_quax.analysis.usage import assess
from dax_quax.cli import main

_MB = 1024 * 1024
ROWSETS = "tests/fixtures/synthetic/rowsets.json"
REPORT = "tests/fixtures/synthetic_pbip"


def snapshot(**kwargs) -> Baseline:
    """A baseline built by hand, so a test can state exactly what differs."""
    objects = kwargs.pop("objects", {})
    base = Baseline(
        model="Contoso",
        source="pbip",
        taken_at="2026-09-18T00:00:00+00:00",
        scope="1 report scanned",
        reports_scanned=1,
        reports_unmatched=0,
        trustworthy=True,
        has_metrics=True,
        measured_bytes=sum(o.bytes or 0 for o in objects.values()),
    )
    for name, value in kwargs.items():
        setattr(base, name, value)
    base.objects = objects
    return base


def obj(key, verdict="KEEP", size=100, kind="column") -> ObjectSnapshot:
    return ObjectSnapshot(key=key, kind=kind, verdict=verdict, bytes=size)


# -- round trip -------------------------------------------------------------------------


def test_a_baseline_survives_json(synthetic_model):
    original = Baseline.from_findings(synthetic_model, assess(synthetic_model))
    restored = Baseline.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.model == original.model
    assert restored.objects.keys() == original.objects.keys()
    assert restored.measured_bytes == original.measured_bytes
    assert restored.trustworthy == original.trustworthy


def test_a_baseline_records_its_own_scope(model_with_report):
    """Without it a later diff cannot tell a model change from a scope change."""
    base = Baseline.from_findings(model_with_report, assess(model_with_report))
    assert base.reports_scanned == 1
    assert base.trustworthy
    assert base.has_metrics


def test_comparing_a_scan_with_itself_is_no_change(synthetic_model):
    base = Baseline.from_findings(synthetic_model, assess(synthetic_model))
    delta = diff(base, base)
    assert delta.unchanged
    assert delta.describe() == "no change"


# -- what changed -------------------------------------------------------------------------


def test_an_added_object():
    before = snapshot(objects={"Sales[A]": obj("Sales[A]")})
    after = snapshot(
        objects={"Sales[A]": obj("Sales[A]"), "Sales[Notes]": obj("Sales[Notes]", size=40 * _MB)}
    )
    delta = diff(before, after)
    assert [c.key for c in delta.added] == ["Sales[Notes]"]
    assert "40.0 MB" in delta.added[0].describe()


def test_a_removed_object():
    before = snapshot(objects={"Sales[A]": obj("Sales[A]"), "Sales[B]": obj("Sales[B]")})
    after = snapshot(objects={"Sales[A]": obj("Sales[A]")})
    assert [c.key for c in diff(before, after).gone] == ["Sales[B]"]


def test_a_verdict_change():
    before = snapshot(objects={"Sales[A]": obj("Sales[A]", "KEEP")})
    after = snapshot(objects={"Sales[A]": obj("Sales[A]", "REMOVE")})
    change = diff(before, after).verdict_changes[0]
    assert (change.was, change.now) == ("KEEP", "REMOVE")
    assert "KEEP -> REMOVE" in change.describe()


def test_a_size_change_without_a_verdict_change():
    before = snapshot(objects={"Sales[A]": obj("Sales[A]", size=1000)})
    after = snapshot(objects={"Sales[A]": obj("Sales[A]", size=3000)})
    delta = diff(before, after)
    assert delta.verdict_changes == ()
    assert delta.size_changes[0].bytes_delta == 2000
    assert delta.growth_bytes == 2000


def test_growth_across_the_whole_model():
    before = snapshot(objects={"a": obj("a", size=10 * _MB)})
    after = snapshot(objects={"a": obj("a", size=10 * _MB), "b": obj("b", size=300 * _MB)})
    assert diff(before, after).growth_bytes == 300 * _MB


def test_a_rename_is_declared_rather_than_guessed():
    """Nothing in a snapshot survives a rename, so the diff says so instead of pretending."""
    before = snapshot(objects={"Sales[Old]": obj("Sales[Old]")})
    after = snapshot(objects={"Sales[New]": obj("Sales[New]")})
    delta = diff(before, after)
    assert len(delta.added) == 1 and len(delta.gone) == 1
    assert any("renamed object" in note for note in delta.notes)


# -- refusing meaningless comparisons ---------------------------------------------------------


def test_verdicts_are_incomparable_when_the_scans_saw_different_reports():
    """Otherwise a scan that simply looked at less reports objects as newly removable."""
    before = snapshot(reports_scanned=2, objects={"a": obj("a", "KEEP")})
    after = snapshot(reports_scanned=1, objects={"a": obj("a", "REMOVE")})
    delta = diff(before, after)
    assert not delta.verdicts_comparable
    assert any("a different amount was looked at" in note for note in delta.notes)


def test_verdicts_are_incomparable_when_unmatched_counts_differ():
    before = snapshot(reports_unmatched=0, objects={"a": obj("a", "REMOVE")})
    after = snapshot(reports_unmatched=1, trustworthy=False, objects={"a": obj("a", "UNKNOWN")})
    assert not diff(before, after).verdicts_comparable


def test_sizes_are_incomparable_when_one_side_has_no_metrics():
    """A PBIP without its cache would otherwise read as the whole model vanishing."""
    before = snapshot(has_metrics=True, objects={"a": obj("a", size=5000)})
    after = snapshot(has_metrics=False, objects={"a": obj("a", size=None)})
    delta = diff(before, after)
    assert not delta.sizes_comparable
    assert delta.size_changes == ()
    assert any("no storage metrics" in note for note in delta.notes)


def test_comparing_two_different_models_is_flagged():
    before = snapshot(model="Contoso", objects={})
    after = snapshot(model="Regional", objects={})
    assert any("unlikely to mean anything" in note for note in diff(before, after).notes)


# -- gates ---------------------------------------------------------------------------------------


def test_no_gates_never_fails():
    before = snapshot(objects={"a": obj("a", "KEEP")})
    after = snapshot(objects={"a": obj("a", "REMOVE")})
    assert diff(before, after).failures(Gates()) == []


def test_new_remove_fails_the_gate():
    before = snapshot(objects={"a": obj("a", "KEEP")})
    after = snapshot(objects={"a": obj("a", "REMOVE")})
    failures = diff(before, after).failures(Gates(fail_on_new_remove=True))
    assert len(failures) == 1
    assert "newly report REMOVE" in failures[0]


def test_an_added_object_that_is_already_removable_counts():
    """Someone committing a dead column is the case this gate is for."""
    before = snapshot(objects={})
    after = snapshot(objects={"Sales[Dead]": obj("Sales[Dead]", "REMOVE")})
    assert diff(before, after).failures(Gates(fail_on_new_remove=True))


def test_an_object_becoming_keep_does_not_fail():
    before = snapshot(objects={"a": obj("a", "REMOVE")})
    after = snapshot(objects={"a": obj("a", "KEEP")})
    assert diff(before, after).failures(Gates(fail_on_new_remove=True)) == []


def test_growth_over_the_limit_fails():
    before = snapshot(objects={"a": obj("a", size=10 * _MB)})
    after = snapshot(objects={"a": obj("a", size=60 * _MB)})
    delta = diff(before, after)
    assert delta.failures(Gates(max_growth_bytes=20 * _MB))
    assert delta.failures(Gates(max_growth_bytes=100 * _MB)) == []


def test_shrinking_never_fails_the_growth_gate():
    before = snapshot(objects={"a": obj("a", size=60 * _MB)})
    after = snapshot(objects={"a": obj("a", size=10 * _MB)})
    assert diff(before, after).failures(Gates(max_growth_bytes=0)) == []


def test_scope_loss_fails_when_asked():
    """A deleted or renamed report looks exactly like this."""
    before = snapshot(trustworthy=True, objects={})
    after = snapshot(trustworthy=False, reports_unmatched=1, objects={})
    delta = diff(before, after)
    assert delta.scope_lost
    failures = delta.failures(Gates(fail_on_scope_loss=True))
    assert "scope narrowed" in failures[0]


# -- THE IMPORTANT ONE: a gate that cannot run must not pass------------------------------------


def test_new_remove_gate_fails_when_verdicts_are_not_comparable():
    """A green build because the check could not run is worse than no check."""
    before = snapshot(reports_scanned=2, objects={"a": obj("a", "KEEP")})
    after = snapshot(reports_scanned=1, objects={"a": obj("a", "KEEP")})
    failures = diff(before, after).failures(Gates(fail_on_new_remove=True))
    assert len(failures) == 1
    assert "cannot be evaluated" in failures[0]
    assert "did not see the same reports" in failures[0] or "looked at" in failures[0]


def test_growth_gate_fails_when_sizes_are_not_comparable():
    before = snapshot(has_metrics=True, objects={"a": obj("a", size=10)})
    after = snapshot(has_metrics=False, objects={"a": obj("a", size=None)})
    failures = diff(before, after).failures(Gates(max_growth_bytes=0))
    assert "cannot be evaluated" in failures[0]


def test_both_gates_report_separately_when_neither_can_run():
    before = snapshot(reports_scanned=2, has_metrics=True, objects={})
    after = snapshot(reports_scanned=1, has_metrics=False, objects={})
    failures = diff(before, after).failures(
        Gates(fail_on_new_remove=True, max_growth_bytes=0)
    )
    assert len(failures) == 2


# -- CLI----------------------------------------------------------------------------------------


def test_cli_saves_and_compares(tmp_path, capsys):
    base = tmp_path / "base.json"
    argv = ["scan", "--rowsets", ROWSETS, "--report-folder", REPORT]
    assert main([*argv, "--save", str(base)]) == 0
    assert base.is_file()
    capsys.readouterr()

    assert main([*argv, "--compare", str(base)]) == 0
    assert "no change" in capsys.readouterr().out


def test_cli_gate_passes_when_nothing_regressed(tmp_path, capsys):
    base = tmp_path / "base.json"
    argv = ["scan", "--rowsets", ROWSETS, "--report-folder", REPORT]
    main([*argv, "--save", str(base)])
    capsys.readouterr()
    assert main([*argv, "--compare", str(base), "--fail-on-new-remove"]) == 0


def test_cli_gate_fails_when_a_report_goes_missing(tmp_path, capsys):
    """The baseline saw a report; this scan does not. Verdicts are not comparable, so the
    gate cannot run — and therefore must fail rather than quietly pass."""
    base = tmp_path / "base.json"
    main(["scan", "--rowsets", ROWSETS, "--report-folder", REPORT, "--save", str(base)])
    capsys.readouterr()

    code = main(["scan", "--rowsets", ROWSETS, "--compare", str(base), "--fail-on-new-remove"])
    assert code == 1
    assert "cannot be evaluated" in capsys.readouterr().err


def test_cli_gate_without_a_baseline_fails(capsys):
    """Nothing was compared, so nothing was checked. That is not a pass."""
    code = main(["scan", "--rowsets", ROWSETS, "--fail-on-new-remove"])
    assert code == 1
    assert "no --compare baseline" in capsys.readouterr().err


def test_cli_missing_baseline_file_is_an_error(tmp_path, capsys):
    code = main(["scan", "--rowsets", ROWSETS, "--compare", str(tmp_path / "nope.json")])
    assert code == 1
    assert "no baseline at" in capsys.readouterr().err


def test_cli_scan_without_baseline_flags_is_unaffected(capsys):
    assert main(["scan", "--rowsets", ROWSETS]) == 0
    assert "since" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--fail-on-new-remove", "--fail-on-scope-loss"])
def test_every_gate_flag_is_accepted(flag):
    from dax_quax.cli import build_parser

    args = build_parser().parse_args(["scan", "--rowsets", ROWSETS, flag])
    assert getattr(args, flag.lstrip("-").replace("-", "_"))


def test_the_growth_limit_reads_as_a_size_not_a_fragment():
    """_size() carries its own separator for appending; reusing it standalone leaked it."""
    before = snapshot(objects={"a": obj("a", size=1 * _MB)})
    after = snapshot(objects={"a": obj("a", size=60 * _MB)})
    message = diff(before, after).failures(Gates(max_growth_bytes=10 * _MB))[0]
    assert "over the 10.0 MB limit" in message
    assert ", 10.0 MB" not in message
