"""Real browser regression coverage for issue #1978.

`refresh_details()` in `dashboard.py` used to unconditionally clear and
rebuild every detail-view section on each one-second timer tick, even when
the underlying diagnostic snapshot had not changed. In a real browser this
destroys and recreates DOM nodes every tick, which resets client-side
component state (table pagination) and is observable as flicker, even
though it happens not to move `window.scrollY` by itself in a headless
browser with no user-driven scroll animation in flight.

These tests cross the actual detail-page timer/render boundary: they run
`init_dashboard()` behind a real `uvicorn` server and drive it with a real
headless Chromium tab (no mocks), matching the acceptance scenarios' explicit
requirement that a helper-level test comparing snapshots or constructing the
desired final state directly is not sufficient regression coverage.
"""

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from unittest.mock import MagicMock

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Browser, Page, sync_playwright

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard
from src.auto_coder.execution_trace import EventKind, ExecutionHandle, Outcome, TraceCollector, _current_scope, get_trace_collector
from src.auto_coder.trace_logger import TraceLogger

REPO = "owner/repo"


@pytest.fixture(autouse=True)
def reset_singletons() -> Iterator[None]:
    TraceCollector._instance = None
    TraceLogger._instance = None
    # This file drives real threads/subprocesses (a uvicorn server thread, a
    # headless Chromium process) via Playwright's sync API, whose internal
    # greenlet/thread bridging can leave `execution_trace._current_scope`
    # (a contextvars.ContextVar) non-None after a test even though every
    # ExecutionHandle used here is properly entered/exited. Force it back to
    # unset on both sides so this file can never leak an ambient execution
    # scope into an unrelated test elsewhere in the same pytest process.
    _current_scope.set(None)
    yield
    _current_scope.set(None)
    TraceCollector._instance = None
    TraceLogger._instance = None


def _resolve_chromium_executable() -> Optional[str]:
    """Prefer a pre-installed Chromium under PLAYWRIGHT_BROWSERS_PATH if the
    default revision-matched lookup would miss it (as happens in some
    sandboxed environments that ship one fixed Chromium revision); otherwise
    let Playwright resolve its own default installation.
    """
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        candidate = Path(browsers_path) / "chromium"
        if candidate.exists():
            return str(candidate)
    return None


@pytest.fixture(scope="module")
def dashboard_base_url() -> Iterator[str]:
    """Start init_dashboard() behind a real uvicorn server, once for this
    module: NiceGUI mounts onto a process-wide app singleton that refuses to
    add middleware again once a real ASGI lifespan has started it, so every
    test in this file shares one live server (distinguished by item number)
    rather than each starting its own.

    Module-scoped fixtures are set up before function-scoped ones (per
    pytest's fixture-ordering rules), so this setup/teardown always runs
    while the autouse `mock_sleep_globally` fixture from a per-test setup is
    not yet (or no longer) active, giving the startup/shutdown polling loop
    below real wall-clock sleeps without needing `_use_real_sleep` itself.
    """
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)

    # NiceGUI mounts onto a single process-wide `core.app` singleton (see
    # `nicegui.ui_run_with.run_with`), and Starlette permanently refuses new
    # `add_middleware()` calls once that singleton has actually served a
    # request. `tests/test_dashboard.py::test_dashboard_endpoint_reachable`
    # also does a real (non-mocked) `init_dashboard()` + request in this same
    # process, and whichever test happens to run first in a given shard would
    # otherwise "win" the singleton. Resetting the already-built middleware
    # stack back to unbuilt lets this second real registration proceed too;
    # it re-adds the same fixed set of NiceGUI middlewares, not new ones, so
    # this does not change the app's real routing/behavior.
    from nicegui import core as _nicegui_core

    if _nicegui_core.app.middleware_stack is not None:
        _nicegui_core.app.middleware_stack = None

    init_dashboard(app, engine, REPO)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("dashboard server did not start in time")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/dashboard"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@contextmanager
def _headless_page() -> Iterator[Page]:
    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        executable_path = _resolve_chromium_executable()
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        try:
            browser: Browser = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # pragma: no cover - environment without a usable browser
            pytest.skip(f"no usable headless Chromium in this environment: {exc}")
        try:
            page = browser.new_page(viewport={"width": 900, "height": 400})
            yield page
        finally:
            browser.close()


def _seed_execution(item_number: int, stage_count: int) -> ExecutionHandle:
    """Start one execution with enough stage-result events to make the
    evidence/decision-log tables paginate and the page scrollable."""
    collector = get_trace_collector()
    handle = collector.start_execution(REPO, "pr", item_number, origin="test")
    handle.__enter__()
    for i in range(stage_count):
        collector.record_event(
            EventKind.STAGE_RESULT,
            stage_id=f"pr.stage-{i}",
            origin="test",
            outcome=Outcome.COMPLETED,
            facts={"n": i},
        )
    return handle


def test_unchanged_snapshot_preserves_scroll_dom_identity_and_pagination(_use_real_sleep, _use_real_home, dashboard_base_url) -> None:
    """AS-001 + AS-005: with no new data, two-plus refresh ticks must not
    move the scroll position, must not tear down and recreate already
    rendered table elements, and must not reset table pagination."""
    handle = _seed_execution(101, stage_count=15)
    try:
        with _headless_page() as page:
            page.goto(f"{dashboard_base_url}/detail/pr/101")
            page.wait_for_selector("text=Decision Log", timeout=10000)
            time.sleep(0.3)

            logs_table = page.locator(".q-table__container").nth(1)
            logs_table.locator("button[aria-label='Next page']").last.click()
            time.sleep(0.2)
            assert "pr.stage-0" in logs_table.inner_text(), "expected to be on the older-events page after clicking Next"

            page.evaluate("window.scrollTo(0, 200)")
            scroll_before = page.evaluate("window.scrollY")
            page.evaluate("window.__evidenceTable = document.querySelectorAll('table')[0]")
            page.evaluate("window.__logsTable = document.querySelectorAll('table')[1]")

            time.sleep(2.2)  # >= two 1s timer ticks with an unchanged snapshot

            scroll_after = page.evaluate("window.scrollY")
            assert scroll_after == scroll_before, "an unchanged refresh must not move the document scroll position"

            same_evidence_node = page.evaluate("document.querySelectorAll('table')[0] === window.__evidenceTable")
            same_logs_node = page.evaluate("document.querySelectorAll('table')[1] === window.__logsTable")
            assert same_evidence_node, "evidence table was torn down and recreated on an unchanged tick (flicker)"
            assert same_logs_node, "decision log table was torn down and recreated on an unchanged tick (flicker)"

            assert "pr.stage-0" in logs_table.inner_text(), "table pagination must not reset when nothing changed"
    finally:
        handle.set_outcome(Outcome.COMPLETED)
        handle.__exit__(None, None, None)


def test_new_evidence_updates_without_resetting_scroll_or_pagination(_use_real_sleep, _use_real_home, dashboard_base_url) -> None:
    """AS-002 + AS-005: new events for the followed execution must become
    visible without forcing the viewport to the page origin and without
    resetting the decision log's current pagination page."""
    handle = _seed_execution(102, stage_count=15)
    collector = get_trace_collector()
    try:
        with _headless_page() as page:
            page.goto(f"{dashboard_base_url}/detail/pr/102")
            page.wait_for_selector("text=Decision Log", timeout=10000)
            time.sleep(0.3)

            logs_table = page.locator(".q-table__container").nth(1)
            logs_table.locator("button[aria-label='Next page']").last.click()
            time.sleep(0.2)
            assert "pr.stage-0" in logs_table.inner_text()

            collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.new-stage", origin="test", outcome=Outcome.COMPLETED, facts={"n": 999})
            time.sleep(1.3)  # next timer tick observes the new event

            assert "pr.new-stage" in page.content(), "newly observed evidence must appear by the next successful refresh"
            scroll_after = page.evaluate("window.scrollY")
            assert scroll_after != 0, "an update that adds evidence must not force the document back to the page origin"
            assert "pr.stage-0" in logs_table.inner_text(), "the previously selected pagination page must survive an in-place row update"
            assert "pr.new-stage" not in logs_table.inner_text(), "the new (newest) event belongs on page 1, not the still-selected older page"
    finally:
        handle.set_outcome(Outcome.COMPLETED)
        handle.__exit__(None, None, None)


def test_pinned_execution_survives_new_execution_starting(_use_real_sleep, _use_real_home, dashboard_base_url) -> None:
    """AS-003: pinning an older execution and scrolling must not be disturbed
    merely because a newer execution starts and periodic refresh observes it."""
    collector = get_trace_collector()

    # Two prior executions: pin the older of the two, then Follow-latest's
    # default (the newer of these two) must give way to the pin.
    old_handle = _seed_execution(105, stage_count=3)
    old_handle.set_outcome(Outcome.COMPLETED)
    old_handle.__exit__(None, None, None)
    old_execution_id = old_handle.scope.execution_id

    mid_handle = collector.start_execution(REPO, "pr", 105, origin="test")
    mid_handle.__enter__()
    collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.mid-stage", origin="test", outcome=Outcome.COMPLETED, facts={"k": 0}, scope=mid_handle.scope)
    mid_handle.set_outcome(Outcome.COMPLETED)
    mid_handle.__exit__(None, None, None)

    with _headless_page() as page:
        page.goto(f"{dashboard_base_url}/detail/pr/105")
        page.wait_for_selector("text=Execution:", timeout=10000)
        time.sleep(0.3)

        exec_label_default = page.locator("text=Execution:").first.inner_text()
        assert mid_handle.scope.execution_id in exec_label_default, "Follow latest should default to the newest execution"

        # Pin the older execution explicitly.
        page.locator("button:has(i:text('arrow_downward'))").first.click()
        time.sleep(0.3)

        page.evaluate("window.scrollTo(0, 100)")
        scroll_before = page.evaluate("window.scrollY")
        exec_label_before = page.locator("text=Execution:").first.inner_text()
        assert old_execution_id in exec_label_before

        new_handle = collector.start_execution(REPO, "pr", 105, origin="test")
        new_handle.__enter__()
        collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.new-exec-stage", origin="test", outcome=Outcome.COMPLETED, facts={"k": 1}, scope=new_handle.scope)
        new_handle.set_outcome(Outcome.COMPLETED)
        new_handle.__exit__(None, None, None)

        time.sleep(1.3)

        exec_label_after = page.locator("text=Execution:").first.inner_text()
        assert old_execution_id in exec_label_after, "a pinned execution must remain selected when a newer execution starts"
        scroll_after = page.evaluate("window.scrollY")
        assert scroll_after == scroll_before, "the viewport must not be reset merely because newer evidence exists"


def test_snapshot_failure_preserves_rendered_view_and_recovers(_use_real_sleep, _use_real_home, dashboard_base_url) -> None:
    """AS-004: a failed snapshot read must leave the last successfully
    rendered content and the viewport untouched, and a later successful
    refresh must recover without forcing the viewport to the page origin."""
    handle = _seed_execution(104, stage_count=1)
    collector = get_trace_collector()
    real_get_snapshot = collector.get_snapshot
    try:
        with _headless_page() as page:
            page.goto(f"{dashboard_base_url}/detail/pr/104")
            page.wait_for_selector("text=Execution:", timeout=10000)
            time.sleep(0.3)

            page.evaluate("window.scrollTo(0, 150)")
            scroll_before = page.evaluate("window.scrollY")
            exec_label_before = page.locator("text=Execution:").first.inner_text()

            should_fail = {"value": True}

            def flaky_get_snapshot(*args, **kwargs):
                if should_fail["value"]:
                    should_fail["value"] = False
                    raise RuntimeError("simulated snapshot read failure")
                return real_get_snapshot(*args, **kwargs)

            collector.get_snapshot = flaky_get_snapshot
            try:
                time.sleep(1.3)  # the failing tick

                assert page.locator("text=Snapshot read failed").count() > 0
                exec_label_during = page.locator("text=Execution:").first.inner_text()
                assert exec_label_during == exec_label_before, "previously rendered evidence must remain in place after a failed refresh"
                scroll_during = page.evaluate("window.scrollY")
                assert scroll_during == scroll_before, "a failed refresh must not disturb the viewport"

                time.sleep(1.3)  # the recovering tick

                assert page.locator("text=Snapshot read failed").count() == 0, "status must recover on the next successful refresh"
                scroll_after = page.evaluate("window.scrollY")
                assert scroll_after == scroll_before, "recovery must not force the viewport back to the page origin"
            finally:
                collector.get_snapshot = real_get_snapshot
    finally:
        handle.set_outcome(Outcome.COMPLETED)
        handle.__exit__(None, None, None)
