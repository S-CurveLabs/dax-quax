"""Run DAX and capture a server-timings trace from a local instance.

The only part of M7 that needs a live engine. Everything it produces is a list of plain
dictionaries, which ``analysis/timings.py`` folds into numbers — so the arithmetic is
tested offline and this file is the part that had to meet a real instance.

WHY A SERVER TRACE, FILTERED
----------------------------
A trace on the server sees every query from every connection, including the ones Power BI
Desktop fires as you click around. Attributing those to a benchmark would be nonsense, so
events are filtered to the SPID of the connection that ran the query, resolved through
DISCOVER_SESSIONS at trace start.

EVENTS ARRIVE LATE
------------------
This was verified against Power BI Desktop 2.157.1354.0 on 2026-09-18, and the one thing
that mattered was patience. Delivery lags the query by **three to four seconds**. The
original code waited 0.35s, caught the events about half the time, and the intermittency
read as "server traces do not work against Desktop" — a conclusion this file carried as
fact for a while. They work. See QUIET and DEADLINE below.

A trace failure never fails the query: the benchmark returns what it has with an
explanation, because a benchmark reporting no timings beats one reporting wrong ones.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from dax_quax.analysis.timings import Benchmark, Timings, parse_events
from dax_quax.errors import DaxQuaxError
from dax_quax.sources.live import LiveConnection, ensure_amo

__all__ = ["NO_EVENTS", "TraceSession", "benchmark", "clear_cache", "evaluate"]

#: What we want off every event. `Spid` is spelled that way -- not SPID -- and RowNumber
#: is not a TraceColumn at all, which is what the previous list claimed.
_COLUMNS = (
    "EventClass",
    "EventSubclass",
    "TextData",
    "Duration",
    "CpuTime",
    "StartTime",
    "EndTime",
    "Spid",
)

#: Which columns each event class accepts, measured against Power BI Desktop 2.157.1354.0
#: on 2026-09-18 by adding one column at a time and calling Update().
#:
#: This table has to exist because the engine validates the whole trace **server-side at
#: Update()**, not at Columns.Add(). Asking one event for one column it does not have
#: fails the entire trace with "The event Id=9 does not contain the column Id=4", so the
#: old code's per-column `suppress(Exception)` around Columns.Add protected nothing: the
#: add always succeeded and the Update always failed. The result was a benchmark that
#: reported "could not start a trace" and no timings, ever.
_EVENT_COLUMNS: dict[str, tuple[str, ...]] = {
    "QueryBegin": ("EventClass", "EventSubclass", "TextData", "StartTime", "Spid"),
    "QueryEnd": _COLUMNS,
    "VertiPaqSEQueryBegin": ("EventClass", "EventSubclass", "TextData", "StartTime", "Spid"),
    "VertiPaqSEQueryEnd": _COLUMNS,
    "VertiPaqSEQueryCacheMatch": ("EventClass", "EventSubclass", "TextData", "Spid"),
    "DAXQueryPlan": _COLUMNS,
    "DirectQueryEnd": ("EventClass", "TextData", "Duration", "CpuTime", "StartTime",
                       "EndTime", "Spid"),
}

_EVENTS = tuple(_EVENT_COLUMNS)

#: Trace events arrive late. Measured against Power BI Desktop 2.157.1354.0: the last
#: event of a query landed **3.4 to 3.6 seconds** after the query returned, and delivery
#: comes in a burst rather than a trickle.
#:
#: This is the whole reason timings looked impossible. The old code waited 0.35 seconds,
#: which caught the events perhaps half the time and nothing the rest, and the
#: intermittency read as "server traces are not delivered against Desktop". They are. A
#: trace that is waited on properly delivers every time, storage-engine events included.
#:
#: So the wait is not a constant. It runs until this query's QueryEnd has arrived and then
#: QUIET seconds more with nothing new, because storage-engine events can trail the query
#: end. DEADLINE bounds the case where nothing ever comes.
QUIET = 1.0
DEADLINE = 20.0
POLL = 0.1

NO_EVENTS = (
    "No trace events arrived within the deadline. Timings need trace permission on the "
    "instance, which a local Power BI Desktop normally grants. Timings are unavailable "
    "rather than wrong."
)


def clear_cache(connection: LiveConnection, database: str) -> None:
    """Drop the storage-engine cache so the next run is cold.

    Without this a second run measures the cache, not the model.
    """
    connection.execute_non_query(
        "<ClearCache xmlns='http://schemas.microsoft.com/analysisservices/2003/engine'>"
        f"<Object><DatabaseID>{database}</DatabaseID></Object></ClearCache>"
    )


def evaluate(connection: LiveConnection, dax: str) -> list[dict[str, Any]]:
    """Run a DAX query and materialise its rows."""
    return connection.execute(dax)


@dataclass
class TraceSession:
    """A session trace over one connection. Use as a context manager."""

    connection: LiveConnection
    spid: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    _server: Any = None
    _trace: Any = None
    _queue: queue.Queue = field(default_factory=queue.Queue)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __enter__(self) -> TraceSession:
        try:
            self._start()
        except Exception as exc:  # noqa: BLE001 - a trace failure must not fail the query
            self.error = f"could not start a trace ({type(exc).__name__}: {exc})"
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._stop()
        except Exception as exc_:  # noqa: BLE001
            self.error = self.error or f"could not stop the trace ({exc_})"

    # -- lifecycle ---------------------------------------------------------------------

    def _start(self) -> None:
        ensure_amo()
        from Microsoft.AnalysisServices import Server, Trace, TraceColumn, TraceEvent

        # Before the trace exists, not after: resolving the SPID runs a DMV query on this
        # same connection, and a trace already running would record that query's QueryEnd
        # as one of ours.
        if self.spid is None:
            with contextlib.suppress(Exception):
                self.spid = self.connection.session_spid()

        server = Server()
        server.Connect(f"Data Source={self.connection.data_source}")
        self._server = server

        # Named, not auto-named. Power BI Desktop already owns a trace called
        # "Trace", which is what Traces.Add() with no name tries to create first.
        trace = server.Traces.Add(f"dax-quax-{uuid.uuid4().hex[:8]}")
        # `trace.Events` resolves to Component.Events -- the inherited EventHandlerList --
        # and shadows Trace.Events, the TraceEventCollection this needs. Reflection asks
        # for the one declared on Trace. Without it: "'EventHandlerList' object has no
        # attribute 'Add'", which reads like a pythonnet bug rather than a name collision.
        collection = trace.GetType().GetProperty("Events").GetValue(trace)
        for name, columns in _EVENT_COLUMNS.items():
            try:
                event = TraceEvent(getattr(_event_class(), name))
            except AttributeError:
                continue
            for column in columns:
                with contextlib.suppress(AttributeError):
                    event.Columns.Add(getattr(TraceColumn, column))
            collection.Add(event)

        # The delegate is held on self deliberately. `trace.OnEvent += _handler(...)`
        # hands .NET a wrapper that nothing on the Python side references, so the GC is
        # free to collect it and the subscription dies silently -- a trace that starts
        # cleanly, reports no error, and delivers nothing. It works right up until the
        # first collection, which is why one run would time and the next would not.
        self._delegate = _handler(self._on_event)
        trace.OnEvent += self._delegate
        trace.Update()
        trace.Start()
        self._trace = trace
        assert Trace is not None  # imported for the type it registers

    def _stop(self) -> None:
        if self._trace is not None:
            self._trace.Stop()
            self._trace.Drop()
            self._trace = None
        if self._server is not None:
            self._server.Disconnect()
            self._server = None
        self._drain()

    # -- events ------------------------------------------------------------------------

    def _on_event(self, _sender: Any, args: Any) -> None:
        """Called on a .NET trace thread, so it only enqueues."""
        try:
            row = {column: _column_value(args, column) for column in _COLUMNS}
        except Exception as exc:  # noqa: BLE001
            self._queue.put({"EventClass": "__error__", "TextData": str(exc)})
            return
        self._queue.put(row)

    def _drain(self) -> None:
        with self._lock:
            while True:
                try:
                    self.events.append(self._queue.get_nowait())
                except queue.Empty:
                    return

    def collected(
        self,
        *,
        quiet: float = QUIET,
        deadline: float = DEADLINE,
        expect: bool = True,
        since: int = 0,
    ) -> list[dict[str, Any]]:
        """Everything delivered for this session, waiting for the trace to finish.

        ``expect`` is False when the caller only wants whatever has already arrived --
        marking the boundary between two runs, say -- and must not pay the wait.
        """
        if expect:
            self._await_quiet(quiet, deadline, since)
        self._drain()
        if self.spid is None:
            return list(self.events)
        return [
            event
            for event in self.events
            if str(event.get("Spid") or self.spid) == str(self.spid)
        ]

    def _await_quiet(self, quiet: float, deadline: float, since: int = 0) -> None:
        """Block until a QueryEnd has arrived and nothing new has for ``quiet`` seconds.

        Waiting for silence alone is not enough: the first event can be three seconds
        behind the query, so a quiet-only loop returns before anything has started
        arriving. Waiting for QueryEnd alone is not enough either, because the
        storage-engine events for the same query can land after it.
        """
        end = time.time() + deadline
        last_change = time.time()
        counted = len(self.events)
        while time.time() < end:
            self._drain()
            if len(self.events) != counted:
                counted = len(self.events)
                last_change = time.time()
            # `since` matters on the second run of a benchmark: the first run's QueryEnd
            # is still in self.events, and without the offset this returns immediately and
            # reports that no events arrived for a query that was never waited for.
            settled = time.time() - last_change >= quiet
            if settled and any(_is_query_end(event) for event in self.events[since:]):
                return
            time.sleep(POLL)


def _is_query_end(event: dict[str, Any]) -> bool:
    return str(event.get("EventClass") or "").strip().lower().endswith("queryend")


def _event_class() -> Any:
    from Microsoft.AnalysisServices import TraceEventClass

    return TraceEventClass


def _handler(callback: Any) -> Any:
    """Wrap a Python callable as the delegate the event expects."""
    from Microsoft.AnalysisServices import TraceEventHandler

    return TraceEventHandler(callback)


#: Columns whose typed property must be read instead of the indexer.
#:
#: `args[TraceColumn.EventClass]` returns the raw wire value -- 9, 10, 85 -- while
#: `args.EventClass` returns the enum, whose str() is "QueryBegin". Everything downstream
#: matches on names, so reading these through the indexer turns every event into
#: "unrecognised EventClass '9'" and the timings quietly come out empty.
_TYPED_COLUMNS = ("EventClass", "EventSubclass")


def _column_value(args: Any, column: str) -> Any:
    from Microsoft.AnalysisServices import TraceColumn

    if column in _TYPED_COLUMNS:
        value = getattr(args, column, None)
        if value is not None:
            return str(value)
    try:
        return args[getattr(TraceColumn, column)]
    except Exception:  # noqa: BLE001 - the column is simply absent on this event
        return None


# -- benchmarking ---------------------------------------------------------------------------


def benchmark(
    dax: str,
    *,
    port: int,
    database: str | None = None,
    runs: int = 3,
    cold: bool = False,
    label: str | None = None,
) -> Benchmark:
    """Run ``dax`` several times against a local instance and time each run.

    With ``cold``, the cache is cleared before every run, which measures the model. Without
    it, the first run warms the cache and the rest measure the cache — so the two modes
    answer different questions and the result records which one it was.
    """
    result = Benchmark(query=dax, cold=cold, label=label)

    with LiveConnection(f"localhost:{port}", database) as connection:
        catalog = database or connection.catalog_name()
        # One trace for every run, not one per run. A trace stopped and dropped between
        # runs delivered events for the first and nothing afterwards, so a two-run
        # benchmark reported one timing and one "no trace events arrived".
        with TraceSession(connection) as session:
            if session.error:
                result.errors.append(session.error)
            for index in range(runs):
                try:
                    if cold:
                        clear_cache(connection, catalog)
                    # Everything already delivered belongs to an earlier run.
                    mark = len(session.collected(expect=False))
                    evaluate(connection, dax)
                    events = session.collected(since=mark)[mark:]
                    timings = parse_events(events)
                    if timings.total_ms == 0 and not events:
                        result.errors.append(
                            f"run {index + 1}: no trace events arrived. " + NO_EVENTS
                        )
                        continue
                    result.runs.append(timings)
                except DaxQuaxError as exc:
                    result.errors.append(f"run {index + 1}: {exc}")
    return result


def timings_for(dax: str, *, port: int, database: str | None = None) -> Timings:
    """One timed run. A thin wrapper over :func:`benchmark` for a single measurement."""
    result = benchmark(dax, port=port, database=database, runs=1)
    return result.runs[0] if result.runs else Timings(unaccounted=tuple(result.errors))
