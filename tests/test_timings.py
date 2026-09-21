"""Server-timings arithmetic.

Every number here is one someone will use to decide whether an optimisation worked, so the
tests care most about the ways a plausible-looking figure can be wrong: double-counted
storage-engine subclasses, a formula-engine residual that swallows unrecognised events, and
a "win" smaller than the run-to-run variation.
"""

from __future__ import annotations

import pytest

from dax_quax.analysis.timings import (
    Benchmark,
    Timings,
    compare,
    parse_events,
    parse_query_plan,
    summarise,
)


def query_end(duration: int, rows: int | None = None) -> dict:
    return {"EventClass": "QueryEnd", "Duration": duration, "RowNumber": rows}


def scan(duration: int, cpu: int, *, subclass: str = "0", text: str | None = None) -> dict:
    return {
        "EventClass": "VertiPaqSEQueryEnd",
        "EventSubclass": subclass,
        "Duration": duration,
        "CpuTime": cpu,
        "TextData": text,
    }


# -- the split ---------------------------------------------------------------------------------


def test_fe_is_the_residual():
    timings = parse_events([query_end(100), scan(30, 60), scan(10, 20)])
    assert timings.total_ms == 100
    assert timings.se_ms == 40
    assert timings.fe_ms == 60
    assert timings.se_cpu_ms == 80


def test_internal_subclasses_do_not_double_count():
    """Summing every VertiPaqSEQueryEnd subclass roughly doubles storage-engine time."""
    timings = parse_events(
        [query_end(100), scan(40, 80), scan(40, 80, subclass="1"), scan(40, 80, subclass="10")]
    )
    assert timings.se_ms == 40
    assert timings.se_query_count == 1
    assert timings.unaccounted == ()


def test_an_unrecognised_subclass_is_reported_not_silently_counted():
    timings = parse_events([query_end(100), scan(40, 80, subclass="77")])
    assert timings.se_ms == 0
    assert any("77" in note for note in timings.unaccounted)


def test_an_unrecognised_event_class_is_reported():
    timings = parse_events([query_end(50), {"EventClass": "SomethingNew", "Duration": 10}])
    assert any("SomethingNew" in note for note in timings.unaccounted)


def test_fe_never_goes_negative():
    """SE time exceeding total means the filter is wrong, not that FE is -20 ms."""
    assert parse_events([query_end(30), scan(50, 90)]).fe_ms == 0


def test_percentages_and_parallelism():
    timings = parse_events([query_end(200), scan(50, 200)])
    assert timings.se_pct == 25.0
    assert timings.fe_pct == 75.0
    assert timings.parallelism == 4.0


def test_empty_trace_is_all_zeros_not_a_crash():
    timings = parse_events([])
    assert timings.total_ms == 0
    assert timings.fe_ms == 0
    assert timings.parallelism == 0.0


def test_cache_matches_are_counted():
    events = [query_end(10), {"EventClass": "VertiPaqSEQueryCacheMatch"}] * 1
    events += [{"EventClass": "VertiPaqSEQueryCacheMatch"}]
    assert parse_events(events).cache_matches == 2


def test_direct_query_time_counts_as_storage_engine():
    timings = parse_events(
        [query_end(100), {"EventClass": "DirectQueryEnd", "Duration": 70}]
    )
    assert timings.se_ms == 70
    assert timings.fe_ms == 30


def test_numeric_event_class_ids_are_accepted():
    timings = parse_events(
        [{"EventClass": "10", "Duration": 80}, {"EventClass": "83", "EventSubclass": "0",
                                                "Duration": 30, "CpuTime": 60}]
    )
    assert (timings.total_ms, timings.se_ms) == (80, 30)


def test_records_are_read_out_of_the_scan_text():
    timings = parse_events([query_end(50), scan(20, 40, text="SELECT ... #Records=1234")])
    assert timings.se_queries[0].rows == 1234


def test_describe_reads_as_a_sentence():
    text = parse_events([query_end(100), scan(40, 80)]).describe()
    assert "100 ms total" in text
    assert "FE 60 ms" in text


# -- query plans ---------------------------------------------------------------------------------


def test_plan_nesting_by_indent():
    plan = parse_query_plan("AddColumns\n\tScan_Vertipaq\n\t\tCache\nFilter", "logical")
    assert [line.depth for line in plan.lines] == [0, 1, 2, 0]
    assert plan.lines[1].text == "Scan_Vertipaq"


def test_plan_records_and_widest_line():
    plan = parse_query_plan(
        "Spool: #Records=10\n\tScan: #Records=900000\n\tOther: #Records=3", "physical"
    )
    assert plan.widest.records == 900000
    assert plan.kind == "physical"


def test_plan_without_record_counts_has_no_widest():
    assert parse_query_plan("AddColumns\n\tFilter", "logical").widest is None


def test_plans_are_collected_from_the_trace():
    timings = parse_events(
        [
            query_end(10),
            {"EventClass": "DAXQueryPlan", "EventSubclass": "1", "TextData": "AddColumns"},
            {"EventClass": "DAXQueryPlan", "EventSubclass": "2", "TextData": "Spool: #Records=4"},
        ]
    )
    assert [plan.kind for plan in timings.plans] == ["logical", "physical"]


# -- benchmarks ---------------------------------------------------------------------------------


def bench(label: str, totals: list[int], se: int = 10) -> Benchmark:
    result = Benchmark(query="EVALUATE Sales", label=label)
    for total in totals:
        result.runs.append(Timings(total_ms=total, se_ms=se))
    return result


def test_median_not_mean():
    """One stalled run should not move the headline."""
    assert bench("b", [100, 104, 900]).median_total_ms == 104


def test_best_and_spread():
    benchmark = bench("b", [100, 104, 130])
    assert benchmark.best_total_ms == 100
    assert benchmark.spread_ms == 30


def test_an_empty_benchmark_is_not_ok():
    assert not Benchmark(query="x").ok
    assert Benchmark(query="x", runs=[Timings(total_ms=5)], errors=["boom"]).ok is False


def test_summarise_shape():
    payload = summarise(bench("before", [100, 110]))
    assert payload["runs"] == 2
    assert payload["median_total_ms"] == 105


# -- M7 ACCEPTANCE: before and after --------------------------------------------------------------


def test_delta_reports_the_improvement():
    delta = compare(bench("before", [200, 202, 204]), bench("after", [120, 121, 122]))
    assert delta.improved
    assert delta.total_ms == -81
    assert delta.total_pct == pytest.approx(-81 / 202 * 100)
    assert "faster" in delta.describe()


def test_delta_splits_fe_from_se():
    before = Benchmark(query="q", runs=[Timings(total_ms=200, se_ms=150)])
    after = Benchmark(query="q", runs=[Timings(total_ms=120, se_ms=20)])
    delta = compare(before, after)
    assert delta.se_ms == -130
    assert delta.fe_ms == +50  # the work moved into the formula engine
    assert delta.total_ms == -80


def test_a_change_smaller_than_the_noise_says_so():
    """Reporting a 3 ms win on runs that vary by 40 ms is how people fool themselves."""
    delta = compare(bench("before", [100, 140, 120]), bench("after", [117, 100, 135]))
    assert delta.within_noise
    assert "noise" in delta.describe()


def test_a_real_win_is_not_called_noise():
    delta = compare(bench("before", [200, 201, 202]), bench("after", [100, 101, 102]))
    assert not delta.within_noise
    assert "noise" not in delta.describe()


def test_a_regression_is_described_as_slower():
    delta = compare(bench("before", [100, 100, 100]), bench("after", [300, 300, 300]))
    assert not delta.improved
    assert "slower" in delta.describe()


def test_delta_of_a_failed_benchmark_says_so():
    delta = compare(bench("before", [100]), Benchmark(query="q", errors=["no trace"]))
    assert "did not run" in delta.describe()


def test_se_query_count_delta():
    before = Benchmark(
        query="q", runs=[parse_events([query_end(100), scan(10, 10), scan(10, 10)])]
    )
    after = Benchmark(query="q", runs=[parse_events([query_end(50), scan(10, 10)])])
    assert compare(before, after).se_queries == -1


# -- the live half, deselected by default-------------------------------------------------------


@pytest.mark.live
def test_benchmark_against_a_running_instance():
    """M7's acceptance criterion, end to end. Needs Power BI Desktop.

    This was an xfail for a while on the belief that server traces are not delivered
    against Desktop. They are; the code was waiting 0.35s for events that take three to
    four seconds to arrive.
    """
    from dax_quax.sources.live import discover_instances
    from dax_quax.sources.trace import benchmark as run_benchmark

    instances = discover_instances()
    if not instances:
        pytest.skip("no Power BI Desktop instance running")
    # An aggregation, not ROW(): a query that never touches the storage engine produces
    # no VertiPaqSE events, and their absence would look like a broken trace.
    query = "EVALUATE SUMMARIZECOLUMNS('Product'[Product], \"s\", [Sales Amount])"
    result = run_benchmark(query, port=instances[0].port, runs=2)
    if not result.runs and any("Product" in e or "Sales Amount" in e for e in result.errors):
        pytest.skip("the open model is not the demo corpus")
    assert result.ok, result.errors
    assert result.median_total_ms > 0
    assert result.runs[0].unaccounted == (), "the trace contract is wrong"


@pytest.mark.live
def test_a_timed_run_splits_storage_from_formula_engine():
    """The split is the whole point of a trace; a wall clock would give the total alone."""
    from dax_quax.sources.live import discover_instances
    from dax_quax.sources.trace import benchmark as run_benchmark

    instances = discover_instances()
    if not instances:
        pytest.skip("no Power BI Desktop instance running")
    query = "EVALUATE SUMMARIZECOLUMNS('Product'[Product], \"s\", [Sales Amount])"
    result = run_benchmark(query, port=instances[0].port, runs=1, cold=True)
    if not result.runs:
        pytest.skip("the open model is not the demo corpus")
    run = result.runs[0]
    assert run.se_queries, "a cold aggregation must hit the storage engine"
    assert run.total_ms == run.se_ms + run.fe_ms


@pytest.mark.live
def test_the_trace_knows_which_session_is_ours():
    """Unfiltered, a benchmark absorbs the queries Desktop fires as you click around."""
    from dax_quax.sources.live import LiveConnection, discover_instances
    from dax_quax.sources.trace import TraceSession

    instances = discover_instances()
    if not instances:
        pytest.skip("no Power BI Desktop instance running")
    port = instances[0].port
    with LiveConnection(f"localhost:{port}") as connection, TraceSession(connection) as session:
        assert session.error is None
        assert isinstance(session.spid, int)


# -- persistence--------------------------------------------------------------------------------


def test_benchmark_round_trips_through_a_dict():
    """A before/after usually spans a model edit, so "before" has to survive on disk."""
    original = Benchmark(query="EVALUATE Sales", label="before", cold=True)
    original.runs.append(parse_events([query_end(200), scan(50, 100), scan(30, 60)]))
    original.runs.append(parse_events([query_end(210), scan(55, 110)]))

    restored = Benchmark.from_dict(original.to_dict())
    assert restored.query == original.query
    assert restored.cold is True
    assert restored.median_total_ms == original.median_total_ms
    assert restored.median_se_ms == original.median_se_ms
    assert restored.median_fe_ms == original.median_fe_ms
    assert restored.se_query_count == original.se_query_count
    assert restored.spread_ms == original.spread_ms


def test_a_restored_benchmark_compares_the_same():
    before = bench("before", [200, 202, 204])
    after = bench("after", [120, 121, 122])
    direct = compare(before, after)
    restored = compare(Benchmark.from_dict(before.to_dict()), after)
    assert restored.total_ms == direct.total_ms
    assert restored.within_noise == direct.within_noise


def test_from_dict_tolerates_a_truncated_payload():
    restored = Benchmark.from_dict({"query": "x", "runs": [{}]})
    assert restored.median_total_ms == 0
    assert restored.se_query_count == 0


# -- waiting for a trace to finish ------------------------------------------------------


def test_a_query_end_is_recognised_whatever_the_event_is_called():
    from dax_quax.sources.trace import _is_query_end

    assert _is_query_end({"EventClass": "QueryEnd"})
    assert _is_query_end({"EventClass": "VertiPaqSEQueryEnd"})
    assert not _is_query_end({"EventClass": "QueryBegin"})
    assert not _is_query_end({"EventClass": None})
    assert not _is_query_end({})


def test_the_wait_returns_once_the_query_has_ended_and_gone_quiet():
    """It must not return on silence alone: the first event can be seconds behind."""
    import threading
    import time

    from dax_quax.sources.live import LiveConnection
    from dax_quax.sources.trace import TraceSession

    session = TraceSession(LiveConnection("unused"))

    def deliver():
        time.sleep(0.3)
        session._queue.put({"EventClass": "QueryBegin"})
        session._queue.put({"EventClass": "QueryEnd"})

    threading.Thread(target=deliver, daemon=True).start()
    started = time.time()
    session._await_quiet(quiet=0.3, deadline=10.0)
    assert 0.3 <= time.time() - started < 5.0
    assert len(session.events) == 2


def test_the_wait_gives_up_at_the_deadline_rather_than_hanging():
    import time

    from dax_quax.sources.live import LiveConnection
    from dax_quax.sources.trace import TraceSession

    session = TraceSession(LiveConnection("unused"))
    started = time.time()
    session._await_quiet(quiet=0.2, deadline=0.6)
    assert time.time() - started < 3.0
    assert session.events == []
