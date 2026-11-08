"""Explicitly loaded pytest native-phase evidence for test-impact diagnostics.

This plugin is never registered by tests/conftest.py or ordinary CI. Coverage
contexts are produced by pytest-cov, not inferred from these timing records.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCHEMA_VERSION = 1
_PHASES = ("setup", "call", "teardown")


def inventory_digest(nodeids: list[str]) -> str:
    payload = json.dumps(nodeids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _ImpactRecorder:
    def __init__(self) -> None:
        directory = os.environ.get("AUTO_CODER_TEST_IMPACT_DIR")
        self.invocation = os.environ.get("AUTO_CODER_TEST_IMPACT_INVOCATION")
        self.path = Path(directory) / f"pytest-events-{os.getpid()}.jsonl" if directory and self.invocation else None
        self.last_error: str | None = None

    def emit(self, kind: str, **fields: object) -> None:
        if self.path is None:
            return
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"schema_version": SCHEMA_VERSION, "invocation_id": self.invocation, "kind": kind, **fields}, sort_keys=True, ensure_ascii=False) + "\n")
                stream.flush()
        except Exception as exc:
            if self.last_error is None:
                self.last_error = f"{type(exc).__name__}: {exc}"
                print(f"Test-impact diagnostic write failed: {self.last_error}", file=sys.stderr, flush=True)


_RECORDER = _ImpactRecorder()


def pytest_sessionstart(session: pytest.Session) -> None:
    _RECORDER.emit("session_start")


def pytest_collection_finish(session: pytest.Session) -> None:
    nodeids = [item.nodeid for item in session.items]
    _RECORDER.emit("collection", nodeids=nodeids, digest=inventory_digest(nodeids))


def _start(item: pytest.Item, phase: str) -> int:
    _RECORDER.emit("phase_start", nodeid=item.nodeid, phase=phase)
    return time.perf_counter_ns()


def _end(item: pytest.Item, phase: str, start_ns: int) -> None:
    _RECORDER.emit("phase_boundary", nodeid=item.nodeid, phase=phase, observer_duration_ms=(time.perf_counter_ns() - start_ns) / 1_000_000)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    started = _start(item, "setup")
    try:
        yield
    finally:
        _end(item, "setup", started)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    started = _start(item, "call")
    try:
        yield
    finally:
        _end(item, "call", started)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item):
    started = _start(item, "teardown")
    try:
        yield
    finally:
        _end(item, "teardown", started)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when not in _PHASES:
        return
    _RECORDER.emit(
        "phase_report",
        nodeid=report.nodeid,
        phase=report.when,
        outcome=report.outcome,
        duration_ms=report.duration * 1000,
        wasxfail=getattr(report, "wasxfail", None),
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    _RECORDER.emit("session_finish", exit_code=int(exitstatus))


def pytest_terminal_summary(terminalreporter: object) -> None:
    if _RECORDER.last_error is not None and hasattr(terminalreporter, "write_line"):
        terminalreporter.write_line(f"Test-impact diagnostic unavailable: {_RECORDER.last_error}", red=True)
