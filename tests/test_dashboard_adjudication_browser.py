"""Mounted browser regressions for the review-adjudication operator state."""

import asyncio
import threading
import time
from typing import Iterator

import pytest
import uvicorn
from fastapi import FastAPI, Request

from src.auto_coder.dashboard_adjudication_ui import register_adjudication_page
from tests.support.browser_launch import headless_page

pytestmark = pytest.mark.browser
REPO = "owner/repo"
CONTEXT = "11111111-1111-4111-8111-111111111111"
TIP_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TIP_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _finding() -> dict:
    return {
        "context_id": CONTEXT,
        "root_comment_id": 100,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "base_ref": "main",
        "contract_digest": "c" * 64,
        "contracts": [{"issue_number": 9, "requirements": [{"id": "REQ-001", "text": "Keep it stable."}]}],
        "status": "CONFLICT",
        "reason": "two current tips conflict",
        "tips": [TIP_A, TIP_B],
        "retired_reason": None,
        "raw_finding": "<script>not executable</script>",
        "observation_revision": "observed-7",
        "decisions": [
            {
                "decision_id": TIP_A,
                "verdict": "UPHOLD",
                "directive": "FIX",
                "rationale": "first rationale",
                "actor_id": 501,
                "actor_url": "https://api.github.com/user/501",
                "comment_id": 701,
                "comment_url": "https://github.com/owner/repo/pull/42#discussion_r701",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "decision_id": TIP_B,
                "verdict": "OVERRULE",
                "directive": "NO_CHANGE",
                "rationale": "second rationale",
                "actor_id": 502,
                "actor_url": "https://api.github.com/user/502",
                "comment_id": 702,
                "comment_url": "https://github.com/owner/repo/pull/42#discussion_r702",
                "created_at": "2026-01-02T00:00:00Z",
            },
        ],
        "processing": None,
    }


@pytest.fixture(scope="module")
def adjudication_browser_server() -> Iterator[tuple[str, dict]]:
    app = FastAPI()
    state = {"drafts": 0, "submits": [], "status_reads": []}

    @app.get("/dashboard-adjudication/availability")
    async def availability():
        return {"repository": REPO, "configured": True, "diagnostic": "ready"}

    @app.get("/dashboard-adjudication/session")
    async def session():
        return {
            "publisher": {"id": 555, "login": "publisher"},
            "configuration_valid": True,
            "authorization_valid": True,
            "authorization_reason": "authorized",
        }

    @app.get("/dashboard-adjudication/context/{pr_number}")
    async def context(pr_number: int):
        return {"pr_number": pr_number, "findings": [_finding()]}

    @app.post("/dashboard-adjudication/draft")
    async def draft(request: Request):
        payload = await request.json()
        state["drafts"] += 1
        await asyncio.sleep(0.25)
        return {
            "decision_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "context_id": CONTEXT,
            "root_comment_id": 100,
            "head_sha": "a" * 40,
            "contract_digest": "c" * 64,
            "tips": [TIP_A, TIP_B],
            "proposed_body": f"server preview: {payload['rationale']}",
        }

    @app.post("/dashboard-adjudication/submit")
    async def submit(request: Request):
        payload = await request.json()
        state["submits"].append(payload)
        return {"status": "outcome-unknown", "decision_id": payload["decision_id"], "github_comment_id": None}

    @app.get("/dashboard-adjudication/status/{decision_id}")
    async def status(decision_id: str):
        state["status_reads"].append(decision_id)
        return {"state": "outcome-unknown", "decision_id": decision_id, "github_comment_id": None}

    from nicegui import core as nicegui_core
    from nicegui import ui

    if nicegui_core.app.middleware_stack is not None:
        nicegui_core.app.middleware_stack = None
    register_adjudication_page(REPO)
    ui.run_with(app, mount_path="/dashboard")
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
            raise RuntimeError("adjudication browser server did not start")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/dashboard", state
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_conflict_history_has_context_decision_actor_and_comment_links(_use_real_sleep, adjudication_browser_server) -> None:
    base_url, _state = adjudication_browser_server
    with headless_page(viewport={"width": 1200, "height": 900}) as page:
        page.goto(f"{base_url}/adjudication/pr/42")
        page.wait_for_selector(f"text=Reader-issued context identity: {CONTEXT}")
        assert page.locator(f"text=decision {TIP_A}").count() == 1
        assert page.locator("a[href='https://api.github.com/user/501']").count() == 1
        assert page.locator("a[href*='discussion_r701']").count() == 1
        assert page.locator("script", has_text="not executable").count() == 0


def test_rationale_change_during_preview_invalidates_confirmation(_use_real_sleep, adjudication_browser_server) -> None:
    base_url, state = adjudication_browser_server
    state["submits"].clear()
    with headless_page(viewport={"width": 1200, "height": 900}) as page:
        page.goto(f"{base_url}/adjudication/pr/43")
        rationale = page.locator("textarea")
        rationale.fill("original rationale")
        page.get_by_role("button", name="Prepare confirmation preview").click()
        rationale.fill("edited while pending")
        page.wait_for_selector("text=Rationale changed while preparing the preview")
        assert page.get_by_role("button", name="Confirm and publish GitHub reply").is_hidden()
        assert state["submits"] == []


def test_refresh_preserves_rationale_but_requires_fresh_preview(_use_real_sleep, adjudication_browser_server) -> None:
    base_url, _state = adjudication_browser_server
    with headless_page(viewport={"width": 1200, "height": 900}) as page:
        page.goto(f"{base_url}/adjudication/pr/44")
        rationale = page.locator("textarea")
        rationale.fill("keep this rationale")
        page.get_by_role("button", name="Refresh authoritative GitHub observations").click()
        page.wait_for_timeout(150)
        assert page.locator("textarea").input_value() == "keep this rationale"
        assert page.get_by_role("button", name="Confirm and publish GitHub reply").is_hidden()


def test_reload_recovers_original_attempt_without_submit_or_draft(_use_real_sleep, adjudication_browser_server) -> None:
    base_url, state = adjudication_browser_server
    decision_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    state["drafts"] = 0
    state["submits"].clear()
    state["status_reads"].clear()
    with headless_page(viewport={"width": 1200, "height": 900}) as page:
        page.goto(f"{base_url}/adjudication/pr/45")
        page.evaluate("id => sessionStorage.setItem('adj-publication-45', id)", decision_id)
        page.reload()
        page.wait_for_selector(f"text=Recovered publication outcome-unknown for decision {decision_id}")
        assert state["status_reads"] == [decision_id]
        assert state["drafts"] == 0
        assert state["submits"] == []
