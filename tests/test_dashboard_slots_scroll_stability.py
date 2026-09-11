"""Real browser regression coverage for the Implementation Slots panel
(Issue #1993, REQ-005/REQ-006).

Mirrors `tests/test_dashboard_detail_scroll_stability.py`'s approach for
the detail view: these tests cross the actual main-page timer/render
boundary behind a real `uvicorn` server and a real headless Chromium tab
(no mocks for the render path), because a helper-level test comparing
projected values or asserting the timer/page was registered cannot show
that an unchanged or slow observation leaves the mounted page undisturbed.

One server/engine is shared for the whole module (see
`test_dashboard_detail_scroll_stability.py` for why: NiceGUI mounts onto a
process-wide `core.app` singleton that only accepts real registration
once); each test uses its own Issue-number range so they cannot interfere
with each other's assertions.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Tuple
from unittest.mock import MagicMock

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Browser, Page, sync_playwright

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard
from src.auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotSnapshotUnavailable

REPO = "owner/repo"


def _resolve_chromium_executable() -> Optional[str]:
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        candidate = Path(browsers_path) / "chromium"
        if candidate.exists():
            return str(candidate)
    return None


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
            page = browser.new_page(viewport={"width": 900, "height": 500})
            yield page
        finally:
            browser.close()


@pytest.fixture(scope="module")
def dashboard_env() -> Iterator[Tuple[str, AutomationEngine, ImplementationSlotRepository]]:
    """One real (non-mocked) `init_dashboard()` behind one real uvicorn
    server for this whole module; see the module docstring for why this
    must not be repeated per-test.

    `ImplementationSlotRepository` resolves its coordination-lock path once
    at construction time from `AUTO_CODER_RUNTIME_ROOT`/`HOME`
    (`runtime_locks.runtime_root()`), but re-derives the same root again on
    every lock acquisition. The per-test `_clear_sensitive_env` autouse
    fixture in `conftest.py` repoints `HOME` to a fresh directory for each
    test function, which runs *after* this module-scoped fixture -- so a
    repository constructed here before that first per-test patch would
    otherwise see its lock path drift out from under it a test later. Pin
    `AUTO_CODER_RUNTIME_ROOT` explicitly for this module's lifetime so the
    resolved root stays fixed regardless of per-test `HOME` churn.
    """
    tmp_dir = Path(tempfile.mkdtemp())
    previous_runtime_root = os.environ.get("AUTO_CODER_RUNTIME_ROOT")
    os.environ["AUTO_CODER_RUNTIME_ROOT"] = str(tmp_dir / "runtime-root")
    try:
        slots = ImplementationSlotRepository(REPO, 30, tmp_dir / "slots.json")
        engine = AutomationEngine(MagicMock())
        engine.implementation_slots = slots

        app = FastAPI()

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
            yield f"http://127.0.0.1:{port}/dashboard", engine, slots
        finally:
            server.should_exit = True
            thread.join(timeout=5)
    finally:
        if previous_runtime_root is None:
            os.environ.pop("AUTO_CODER_RUNTIME_ROOT", None)
        else:
            os.environ["AUTO_CODER_RUNTIME_ROOT"] = previous_runtime_root


@contextmanager
def _standalone_dashboard_env(repo_name: str) -> Iterator[Tuple[str, AutomationEngine, ImplementationSlotRepository]]:
    """Like `dashboard_env`, but a fresh, fully isolated engine/server used
    by exactly one test.

    NiceGUI's `reconnect_timeout` (a few seconds) keeps a disconnected
    client's session -- and its `ui.timer` -- alive for a grace period
    after its browser closes. A test that globally monkeypatches a pure
    function shared by every client (e.g. `dashboard_slots.summarize`)
    cannot tell its own client's calls apart from a still-draining client
    left over from an earlier test on the same shared server, so it needs
    its own server rather than the module-scoped `dashboard_env`.
    """
    tmp_dir = Path(tempfile.mkdtemp())
    previous_runtime_root = os.environ.get("AUTO_CODER_RUNTIME_ROOT")
    os.environ["AUTO_CODER_RUNTIME_ROOT"] = str(tmp_dir / "runtime-root")
    try:
        slots = ImplementationSlotRepository(repo_name, 30, tmp_dir / "slots.json")
        engine = AutomationEngine(MagicMock())
        engine.implementation_slots = slots

        app = FastAPI()

        from nicegui import core as _nicegui_core

        if _nicegui_core.app.middleware_stack is not None:
            _nicegui_core.app.middleware_stack = None

        init_dashboard(app, engine, repo_name)

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
            yield f"http://127.0.0.1:{port}/dashboard", engine, slots
        finally:
            server.should_exit = True
            thread.join(timeout=5)
    finally:
        if previous_runtime_root is None:
            os.environ.pop("AUTO_CODER_RUNTIME_ROOT", None)
        else:
            os.environ["AUTO_CODER_RUNTIME_ROOT"] = previous_runtime_root


def test_unchanged_slot_snapshot_preserves_scroll_and_dom_identity(_use_real_sleep, _use_real_home, dashboard_env) -> None:
    """AS-005: with no new slot data, two-plus refresh ticks must not move
    the scroll position or tear down and recreate already-rendered owner
    rows."""
    base_url, _engine, slots = dashboard_env
    for i in range(12):
        owner = ImplementationOwner("issue", 9100 + i)
        assert slots.start_execution(owner) is not None

    with _headless_page() as page:
        page.goto(f"{base_url}/")
        page.wait_for_selector("text=Implementation Slots", timeout=10000)
        page.wait_for_selector("text=Issue #9100", timeout=10000)
        time.sleep(0.3)

        page.evaluate("window.scrollTo(0, 200)")
        scroll_before = page.evaluate("window.scrollY")
        page.evaluate("window.__firstOwnerCard = document.querySelectorAll('.q-card')[0]")

        time.sleep(2.2)  # >= two 1s timer ticks with an unchanged snapshot

        scroll_after = page.evaluate("window.scrollY")
        assert scroll_after == scroll_before, "an unchanged slot refresh must not move the document scroll position"

        same_card_node = page.evaluate("document.querySelectorAll('.q-card')[0] === window.__firstOwnerCard")
        assert same_card_node, "owner rows were torn down and recreated on an unchanged tick (flicker)"
        assert "Issue #9100" in page.content()
        assert "Issue #9111" in page.content()


def test_membership_update_refreshes_without_full_panel_rebuild(_use_real_sleep, _use_real_home, dashboard_env) -> None:
    """AS-005: a membership-only change (a newly recorded PR for an existing
    owner) must become visible without resetting the scroll position, and
    without tearing down an unrelated owner's row that did not change."""
    base_url, _engine, slots = dashboard_env
    anchor_owner = ImplementationOwner("issue", 9200)
    sibling_owner = ImplementationOwner("issue", 9201)
    assert slots.start_execution(anchor_owner) is not None
    assert slots.start_execution(sibling_owner) is not None

    with _headless_page() as page:
        page.goto(f"{base_url}/")
        page.wait_for_selector("text=Issue #9200", timeout=10000)
        time.sleep(0.3)

        page.evaluate("window.scrollTo(0, 50)")
        scroll_before = page.evaluate("window.scrollY")
        page.evaluate(
            """
            window.__siblingCard = Array.from(document.querySelectorAll('.q-card'))
              .find(el => el.textContent.includes('Issue #9201'));
            """
        )

        assert slots.record_implementation_pr(anchor_owner, 9299)
        time.sleep(1.3)  # next timer tick observes the new membership

        assert "#9299" in page.content(), "a newly recorded PR must appear by the next successful refresh"
        scroll_after = page.evaluate("window.scrollY")
        assert scroll_after == scroll_before, "a membership-only update must not reset the viewport"
        same_sibling_card = page.evaluate(
            """
            window.__siblingCard === Array.from(document.querySelectorAll('.q-card'))
              .find(el => el.textContent.includes('Issue #9201'));
            """
        )
        assert same_sibling_card, "an unrelated owner's row must not be torn down by another owner's membership update"


def test_slow_slot_observation_does_not_block_other_status_refresh(_use_real_sleep, _use_real_home, dashboard_env) -> None:
    """AS-005: a slow/delayed observation boundary must not block this
    page's event loop or pause the Active Workers/Queue/Open Items refresh,
    which runs on its own independent `ui.timer`."""
    base_url, engine, slots = dashboard_env
    assert slots.start_execution(ImplementationOwner("issue", 9300)) is not None
    release = threading.Event()
    real_get_snapshot = engine.get_implementation_slot_snapshot

    def _delayed_get_snapshot(repo_name: str):
        release.wait(timeout=5)
        return real_get_snapshot(repo_name)

    engine.get_implementation_slot_snapshot = _delayed_get_snapshot
    try:
        with _headless_page() as page:
            page.goto(f"{base_url}/")
            page.wait_for_selector("text=Loading implementation slot occupancy", timeout=10000)

            # While the slot observation is still blocked, the independent
            # Workers/Queue/Open Items refresh (its own `ui.timer`) must
            # still have run: "No active workers" only appears once
            # `refresh_status()` has executed at least once.
            page.wait_for_selector("text=No active workers", timeout=5000)

            # The page must stay responsive (not hung waiting on the slow
            # slot observation): a script evaluation completes promptly.
            assert page.evaluate("1 + 1") == 2
            # The panel must not fabricate a known/empty state while the
            # observation is still pending.
            assert "Issue #9300" not in page.content()
    finally:
        release.set()
        engine.get_implementation_slot_snapshot = real_get_snapshot


def test_owner_and_pr_links_navigate_to_the_correct_detail_page(_use_real_sleep, _use_real_home, dashboard_env) -> None:
    """AS-001: owner/PR links must actually navigate to the correct
    repository-scoped detail target when followed, not just carry a
    plausible-looking label (a mocked `ui.link` call cannot establish
    this -- only a real click and a real resulting page can)."""
    base_url, _engine, slots = dashboard_env
    owner = ImplementationOwner("issue", 9400)
    assert slots.start_execution(owner) is not None
    assert slots.record_implementation_pr(owner, 9401)

    with _headless_page() as page:
        page.goto(f"{base_url}/")
        page.wait_for_selector("text=Issue #9400", timeout=10000)

        page.click("text=Issue #9400")
        page.wait_for_selector("text=Detail View: Issue #9400", timeout=10000)
        assert page.url.endswith("/detail/issue/9400")

        page.go_back()
        page.wait_for_selector("text=Issue #9400", timeout=10000)
        page.click("text=#9401")
        page.wait_for_selector("text=Detail View: Pr #9401", timeout=10000)
        assert page.url.endswith("/detail/pr/9401")


def test_render_failure_does_not_leave_a_mixed_partial_dom_state(_use_real_sleep, _use_real_home) -> None:
    """REQ-004/REQ-006: a transient rendering failure must not leave a
    mixed partial DOM (a new owner visible while counters/banner still
    reflect the old state, or vice versa) -- the panel must show the
    coherent last-known state with a stale indication instead, then
    recover cleanly on the next successful render.

    Uses its own standalone server (not the shared `dashboard_env`): this
    test globally monkeypatches `dashboard_slots.summarize`, and a client
    left over from an earlier test (draining during NiceGUI's
    `reconnect_timeout` grace period after its browser closed) would
    otherwise also observe the patch and race this test's own client for
    which one "first" sees the newly added owner.
    """
    with _standalone_dashboard_env("owner/repo-render-failure") as (base_url, _engine, slots):
        anchor = ImplementationOwner("issue", 9450)
        assert slots.start_execution(anchor) is not None

        with _headless_page() as page:
            page.goto(f"{base_url}/")
            page.wait_for_selector("text=Issue #9450", timeout=10000)
            time.sleep(0.3)
            assert "Issue #9451" not in page.content()

            new_owner = ImplementationOwner("issue", 9451)
            assert slots.start_execution(new_owner) is not None

            from src.auto_coder import dashboard_slots as dashboard_slots_module

            real_summarize = dashboard_slots_module.summarize
            raised_once = {"done": False}

            def flaky_summarize(snapshot):
                # The dashboard's own `ui.timer` runs in a separate server
                # thread with no synchronization against this test's writes,
                # so tie the one simulated failure to *observing owner 9451
                # for the first time* (a data condition) rather than to an
                # absolute call count -- otherwise which tick actually sees
                # the new owner races against when this patch takes effect.
                has_new_owner = any(o.number == 9451 for o in snapshot.owners)
                if has_new_owner and not raised_once["done"]:
                    raised_once["done"] = True
                    raise RuntimeError("simulated transient rendering failure")
                return real_summarize(snapshot)

            dashboard_slots_module.summarize = flaky_summarize
            try:
                # Poll (rather than a single fixed sleep) until the simulated
                # failure has actually fired, since it races an independent
                # server-thread timer tick.
                for _ in range(50):
                    if raised_once["done"]:
                        break
                    time.sleep(0.1)
                assert raised_once["done"], "the simulated failure never fired -- the server thread never observed owner 9451"
                time.sleep(0.2)  # let the compensating render (same tick) finish

                content_during_failure = page.content()
                assert "Issue #9451" not in content_during_failure, "a partially-rendered new owner must not remain visible after a failed render"
                assert "Issue #9450" in content_during_failure
                assert page.locator("text=rendering failed").count() > 0, "a rendering failure must surface as a slot-panel diagnostic"

                time.sleep(1.3)  # the recovering tick (flaky_summarize now delegates to the real implementation)
            finally:
                dashboard_slots_module.summarize = real_summarize

            content_after_recovery = page.content()
            assert "Issue #9451" in content_after_recovery, "the new owner must appear once rendering succeeds again"
            assert page.locator("text=rendering failed").count() == 0


def test_slot_observation_is_single_flight_across_timer_ticks(_use_real_sleep, _use_real_home) -> None:
    """REQ-005/AS-005: `refresh_slots`'s own-tick guard
    (`slots_state["refreshing"]`) must actually prevent an overlapping
    observation-boundary call while a real observation is slow -- not just
    be presumed correct because it reads a shared dict. Holds the real
    observation boundary blocked across several elapsed 1s timer ticks and
    counts entries/concurrency at that exact boundary: if the guard failed
    to suppress a re-entrant tick, a later tick would start its own
    overlapping call while the first was still pending, and this would
    observe `max_concurrent > 1` and/or more than one `entries`. Then
    releases the blocked call and proves the guard is not stuck forever: a
    later tick can still enter the boundary once it is free again.

    Uses its own standalone server (not the shared `dashboard_env`): this
    test globally monkeypatches `engine.get_implementation_slot_snapshot`,
    and a client left over from an earlier test (draining during NiceGUI's
    `reconnect_timeout` grace period after its browser closed) would
    otherwise also invoke the patched function through its own still-running
    timer, inflating the entry/concurrency counts independently of this
    test's own client.
    """
    with _standalone_dashboard_env("owner/repo-single-flight") as (base_url, engine, slots):
        assert slots.start_execution(ImplementationOwner("issue", 9500)) is not None

        lock = threading.Lock()
        state = {"entries": 0, "concurrent": 0, "max_concurrent": 0}
        release_first = threading.Event()
        real_get_snapshot = engine.get_implementation_slot_snapshot

        def _counting_get_snapshot(repo_name: str):
            with lock:
                state["entries"] += 1
                state["concurrent"] += 1
                state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
                entry_index = state["entries"]
            try:
                if entry_index == 1:
                    release_first.wait(timeout=10)
                return real_get_snapshot(repo_name)
            finally:
                with lock:
                    state["concurrent"] -= 1

        engine.get_implementation_slot_snapshot = _counting_get_snapshot
        try:
            with _headless_page() as page:
                page.goto(f"{base_url}/")
                page.wait_for_selector("text=Loading implementation slot occupancy", timeout=10000)

                # Let several 1s timer ticks elapse while the first
                # observation is still blocked.
                time.sleep(3.5)
                with lock:
                    assert state["entries"] == 1, f"expected exactly one in-flight observation after 3+ elapsed ticks while blocked, saw {state['entries']} entries -- " "a later tick re-entered the observation boundary instead of being suppressed by the single-flight guard"
                    assert state["max_concurrent"] == 1, f"observation boundary was entered concurrently: max_concurrent={state['max_concurrent']}"

                # Release the blocked observation and prove the guard is
                # not stuck forever: a later tick can enter the boundary
                # again.
                release_first.set()
                page.wait_for_selector("text=Issue #9500", timeout=10000)
                deadline = time.time() + 5
                while True:
                    with lock:
                        if state["entries"] > 1 or time.time() >= deadline:
                            break
                    time.sleep(0.1)
                with lock:
                    assert state["entries"] > 1, "no later tick re-entered the observation boundary after release"
                    assert state["max_concurrent"] == 1, f"observation boundary was entered concurrently after release: max_concurrent={state['max_concurrent']}"
        finally:
            release_first.set()
            engine.get_implementation_slot_snapshot = real_get_snapshot


def test_returned_unavailable_after_success_preserves_rendered_state(_use_real_sleep, _use_real_home) -> None:
    """REQ-004: a *returned* `ImplementationSlotSnapshotUnavailable` (the
    real `ImplementationSlotRepository.snapshot()` catching a storage fault
    and returning its typed unavailable result, not a raised exception)
    after a prior success must preserve the *actually rendered* owner rows
    with a stale indication and the unchanged last-successful-observation
    timestamp -- then, once the file is restored, recovery must show a
    genuinely newer successful timestamp with the stale indication cleared.

    A mock-based equivalent
    (`test_returned_unavailable_after_success_preserves_stale_state` in
    `tests/test_dashboard_slots_observability.py`) cannot prove either
    half: a mocked `ui.link`/`ui.label` container's `call_args_list` never
    shrinks even after the panel `.clear()`s and rebuilds it, so "the link
    is present somewhere in call history" stays true even if a regression
    wiped the panel entirely (that branch doesn't call `ui.link` again
    either, so no new call appears, but the original one from the first
    successful render never leaves the mock's history) -- and nothing
    stops a stale-then-recovered banner from silently keeping the *same*
    timestamp. This drives the real DOM instead, where a cleared element is
    actually gone and the displayed timestamp text is the only source of
    truth.

    Uses its own standalone server (its own `slots.json`): this test
    corrupts the state file's bytes directly, which would otherwise wreck
    the shared `dashboard_env` fixture's file for every other test in this
    module.
    """
    with _standalone_dashboard_env("owner/repo-returned-unavailable") as (base_url, engine, slots):
        owner = ImplementationOwner("issue", 9600)
        assert slots.start_execution(owner) is not None
        state_path = slots.storage_path

        with _headless_page() as page:
            page.goto(f"{base_url}/")
            page.wait_for_selector("text=Issue #9600", timeout=10000)
            page.wait_for_selector("text=Implementation slots as of", timeout=10000)
            success_banner_text = page.locator("text=Implementation slots as of").inner_text()
            success_timestamp = success_banner_text.split("as of ")[1].split(" (local")[0]

            original_bytes = state_path.read_bytes()
            state_path.write_text("{not valid json")
            # Confirm directly (same real adapter the panel's own timer
            # tick uses) that the corrupted file genuinely makes
            # `get_implementation_slot_snapshot` *return*
            # `ImplementationSlotSnapshotUnavailable`, not raise -- the
            # exact branch a mock-only test cannot distinguish from the
            # exception path.
            direct_observation = engine.get_implementation_slot_snapshot("owner/repo-returned-unavailable")
            assert isinstance(direct_observation, ImplementationSlotSnapshotUnavailable)

            page.wait_for_selector("text=STALE", timeout=10000)
            content_during_failure = page.content()
            assert "Issue #9600" in content_during_failure, "the previously rendered owner row must remain actually visible while stale, not just once-created"
            assert success_timestamp in content_during_failure, "the stale banner must keep the original last-successful timestamp"

            time.sleep(1.5)  # ensure the recovered observation's second-granularity timestamp differs
            state_path.write_bytes(original_bytes)

            deadline = time.time() + 10
            while "STALE" in page.content() and time.time() < deadline:
                time.sleep(0.2)
            content_after_recovery = page.content()
            assert "STALE" not in content_after_recovery, "recovery must clear the stale indication"
            assert "Issue #9600" in content_after_recovery

            recovered_banner_text = page.locator("text=Implementation slots as of").inner_text()
            recovered_timestamp = recovered_banner_text.split("as of ")[1].split(" (local")[0]
            assert recovered_timestamp != success_timestamp, "recovery must show a genuinely newer successful-observation timestamp, not silently keep the stale one"
