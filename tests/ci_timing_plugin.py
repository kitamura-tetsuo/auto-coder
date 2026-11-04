"""Pytest integration for the optional CI timing recorder."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest

from tests.fixtures.ci_timing import recorder

_PHASE_SPANS: dict[tuple[str, str], str] = {}


def _phase(item: pytest.Item, when: str):
    timing = recorder()
    span_id = str(uuid.uuid4())
    started = time.perf_counter_ns()
    _PHASE_SPANS[(item.nodeid, when)] = span_id
    timing.emit(
        "pytest_phase_start",
        span_id=span_id,
        parent_span_id=None,
        nodeid=item.nodeid,
        pytest_phase=when,
        started_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    return timing, span_id, started


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    timing, span_id, started = _phase(item, "setup")
    yield
    timing.emit("pytest_phase_boundary", span_id=span_id, nodeid=item.nodeid, pytest_phase="setup", observer_elapsed_ms=(time.perf_counter_ns() - started) / 1_000_000)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    timing, span_id, started = _phase(item, "call")
    yield
    timing.emit("pytest_phase_boundary", span_id=span_id, nodeid=item.nodeid, pytest_phase="call", observer_elapsed_ms=(time.perf_counter_ns() - started) / 1_000_000)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item):
    timing, span_id, started = _phase(item, "teardown")
    yield
    timing.emit("pytest_phase_boundary", span_id=span_id, nodeid=item.nodeid, pytest_phase="teardown", observer_elapsed_ms=(time.perf_counter_ns() - started) / 1_000_000)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    recorder().emit(
        "pytest_phase_report",
        span_id=_PHASE_SPANS.pop((report.nodeid, report.when), None),
        nodeid=report.nodeid,
        pytest_phase=report.when,
        native_duration_ms=report.duration * 1000,
        outcome=report.outcome,
        wasxfail=getattr(report, "wasxfail", None),
        longrepr=str(report.longrepr) if report.failed else None,
    )
