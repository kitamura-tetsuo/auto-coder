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
from src.auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

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


def test_unchanged_slot_snapshot_preserves_scroll_and_dom_identity(_use_real_sleep, dashboard_env) -> None:
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


def test_membership_update_refreshes_without_full_panel_rebuild(_use_real_sleep, dashboard_env) -> None:
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


def test_slow_slot_observation_does_not_block_other_status_refresh(_use_real_sleep, dashboard_env) -> None:
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
