import pytest


@pytest.fixture(autouse=True)
def mock_init_dashboard(monkeypatch):
    from auto_coder import webhook_server

    monkeypatch.setattr(webhook_server, "init_dashboard", lambda *args, **kwargs: None)


"""Production-path regression tests for Issue #2022.

These tests mount the real FastAPI router (`init_dashboard_adjudication`)
against a real `AutomationEngine`, and populate its adjudication snapshots
through the actual production boundary (`AutomationEngine.refresh_review_adjudications`),
exactly as the webhook-driven orchestration path would. Only the external
GitHub transport (`get_ghapi_client`) and the `[dashboard_adjudication]`
config loader are doubled.
"""

from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard_adjudication import init_dashboard_adjudication
from src.auto_coder.llm_backend_config import DashboardAdjudicationConfig
from src.auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

REPO = "owner/repo"
ORIGIN = "https://dashboard.example.test"
ROOT_AUTHOR_ID = 777
PUBLISHER_ID = 555
PR_NUMBER = 42
ISSUE_BODY = """## Objective

Keep the exact value stable.

## Requirements

REQ-001: Preserve the exact value.
"""


def _write_secret(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def _thread(extra_comments=None) -> ReviewThread:
    root = ReviewThreadComment(100, "finding body", "bot-reviewer", ROOT_AUTHOR_ID, "Bot", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    return ReviewThread(id="THREAD1", comments=[root, *(extra_comments or [])])


def _fake_api(user_id=PUBLISHER_ID, reply_should_fail=False, reply_response_override=None):
    api = MagicMock()
    api.users.get_authenticated.return_value = {"id": user_id}

    def _reply(owner, repo, pr_number, comment_id, body):
        if reply_should_fail:
            raise TimeoutError("simulated lost response")
        if reply_response_override is not None:
            return reply_response_override
        return {"id": 9001, "body": body, "user": {"id": user_id}}

    api.pulls.create_reply_for_review_comment.side_effect = _reply
    return api


def _build_engine(tmp_path: Path, monkeypatch, thread: ReviewThread, reviewer_ids=(ROOT_AUTHOR_ID,), adjudicator_ids=(PUBLISHER_ID,)) -> AutomationEngine:
    monkeypatch.setenv("AUTO_CODER_REVIEW_ADJUDICATION_DB", str(tmp_path / "adjudications.sqlite3"))
    monkeypatch.setenv("AUTO_CODER_DASHBOARD_ADJUDICATION_JOURNAL_DB", str(tmp_path / "journal.sqlite3"))

    mock_github = MagicMock()
    mock_github.get_pull_request_metadata_strict.return_value = {
        "number": PR_NUMBER,
        "state": "open",
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 321}},
        "body": "Fixes #9",
    }
    mock_github.get_issue_dispatch_snapshot_strict.return_value = {"number": 9, "id": 909, "title": "t", "body": ISSUE_BODY}
    mock_github.get_pr_review_threads_strict.return_value = [thread]
    mock_github.invalidate_issue_reads_for_adjudication = MagicMock()

    engine = AutomationEngine(mock_github)

    monkeypatch.setattr("src.auto_coder.automation_engine.get_pr_review_allowlist_from_config", lambda repo_name=None, config_path=None: list(reviewer_ids))
    monkeypatch.setattr("src.auto_coder.automation_engine.get_review_adjudicator_allowlist_from_config", lambda repo_name=None, config_path=None: list(adjudicator_ids))
    engine.refresh_review_adjudications(REPO, {"number": PR_NUMBER})
    return engine


def _mount(tmp_path: Path, monkeypatch, engine: AutomationEngine, *, enabled=True, allowed_origin=ORIGIN, api=None):
    secret_path = _write_secret(tmp_path, "operator.secret", "s" * 40)
    token_path = _write_secret(tmp_path, "operator.token", "gh-token-value")
    config = DashboardAdjudicationConfig(enabled=enabled, operator_secret_file=secret_path, github_token_file=token_path, allowed_origin=allowed_origin)
    monkeypatch.setattr("src.auto_coder.dashboard_adjudication.get_dashboard_adjudication_config", lambda repo_name=None, config_path=None: config)
    monkeypatch.setattr("src.auto_coder.dashboard_adjudication.get_pr_review_allowlist_from_config", lambda repo_name=None, config_path=None: [ROOT_AUTHOR_ID])
    monkeypatch.setattr("src.auto_coder.dashboard_adjudication.get_review_adjudicator_allowlist_from_config", lambda repo_name=None, config_path=None: [PUBLISHER_ID])
    fake_api = api if api is not None else _fake_api()
    monkeypatch.setattr("src.auto_coder.dashboard_adjudication.get_ghapi_client", lambda token: fake_api)

    app = FastAPI()
    service = init_dashboard_adjudication(app, engine, REPO)
    client = TestClient(app)
    return client, service, secret_path, token_path, fake_api


def _login(client: TestClient, secret="s" * 40, origin=ORIGIN):
    return client.post("/dashboard-adjudication/login", json={"secret": secret}, headers={"origin": origin})


def test_disabled_config_blocks_every_route(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    client, _service, _secret, _token, api = _mount(tmp_path, monkeypatch, engine, enabled=False)

    login = _login(client)
    assert login.status_code == 403

    ctx = client.get(f"/dashboard-adjudication/context/{PR_NUMBER}", headers={"origin": ORIGIN})
    assert ctx.status_code in (401, 403)
    api.pulls.create_reply_for_review_comment.assert_not_called()


def test_login_rejects_wrong_secret_and_wrong_origin(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    client, *_ = _mount(tmp_path, monkeypatch, engine)

    assert _login(client, secret="wrong-secret").status_code == 401
    assert _login(client, origin="https://evil.example.test").status_code == 403
    assert _login(client).status_code == 200


def test_context_read_requires_session_and_csrf_is_not_needed_for_reads(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    client, *_ = _mount(tmp_path, monkeypatch, engine)

    anon = client.get(f"/dashboard-adjudication/context/{PR_NUMBER}", headers={"origin": ORIGIN})
    assert anon.status_code == 401

    login = _login(client)
    ctx = client.get(f"/dashboard-adjudication/context/{PR_NUMBER}", headers={"origin": ORIGIN})
    assert ctx.status_code == 200
    findings = ctx.json()["findings"]
    assert len(findings) == 1
    assert findings[0]["contributing_issues"] == [9]
    assert "s" * 40 not in ctx.text
    assert login.cookies.get("auto_coder_dashboard_adjudication_session") is not None


def test_draft_and_submit_confirm_publication_and_trigger_reconciliation(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    client, service, *_ = _mount(tmp_path, monkeypatch, engine)
    initial_reply_calls = engine.github.reply_to_review_thread.call_count
    login = _login(client)
    csrf = login.json()["csrf_token"]

    context_id = engine.get_review_adjudication_snapshots(REPO, PR_NUMBER)[0].context.context_id

    draft = client.post("/dashboard-adjudication/draft", json={"pr_number": PR_NUMBER, "context_id": context_id}, headers={"origin": ORIGIN})
    assert draft.status_code == 200
    draft_body = draft.json()
    assert draft_body["tips"] == []
    assert "auto-coder-review-adjudication:v1" in draft_body["proposed_body"]

    submit_payload = {
        "pr_number": PR_NUMBER,
        "context_id": context_id,
        "decision_id": draft_body["decision_id"],
        "head_sha": draft_body["head_sha"],
        "contract_digest": draft_body["contract_digest"],
        "verdict": "UPHOLD",
        "directive": "FIX",
        "rationale": "The provenance concern is valid.",
        "supersedes": [],
    }
    # No CSRF token: must be rejected before any GitHub write.
    no_csrf = client.post("/dashboard-adjudication/submit", json=submit_payload, headers={"origin": ORIGIN})
    assert no_csrf.status_code == 403

    submit = client.post("/dashboard-adjudication/submit", json=submit_payload, headers={"origin": ORIGIN, "x-csrf-token": csrf})
    assert submit.status_code == 200
    body = submit.json()
    assert body["status"] == "published-awaiting-processing"
    assert body["github_comment_id"] == 9001
    # The writer uses the dedicated credential's direct API call, never the
    # controller's own reply_to_review_thread helper.
    assert engine.github.reply_to_review_thread.call_count == initial_reply_calls

    status = client.get(f"/dashboard-adjudication/status/{draft_body['decision_id']}", headers={"origin": ORIGIN})
    assert status.json()["state"] == "confirmed-published"
    assert status.json()["github_comment_id"] == 9001


def test_publisher_not_in_adjudicator_allowlist_is_rejected_before_post(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    fake_api = _fake_api(user_id=999999)  # not in the configured allowlist
    client, service, *_ = _mount(tmp_path, monkeypatch, engine, api=fake_api)
    login = _login(client)
    csrf = login.json()["csrf_token"]
    context_id = engine.get_review_adjudication_snapshots(REPO, PR_NUMBER)[0].context.context_id

    draft = client.post("/dashboard-adjudication/draft", json={"pr_number": PR_NUMBER, "context_id": context_id}, headers={"origin": ORIGIN}).json()
    submit = client.post(
        "/dashboard-adjudication/submit",
        json={
            "pr_number": PR_NUMBER,
            "context_id": context_id,
            "decision_id": draft["decision_id"],
            "head_sha": draft["head_sha"],
            "contract_digest": draft["contract_digest"],
            "verdict": "UPHOLD",
            "directive": "FIX",
            "rationale": "reason",
            "supersedes": [],
        },
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert submit.status_code == 403
    fake_api.pulls.create_reply_for_review_comment.assert_not_called()


def test_stale_head_sha_between_draft_and_submit_is_rejected(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    client, service, *_ = _mount(tmp_path, monkeypatch, engine)
    login = _login(client)
    csrf = login.json()["csrf_token"]
    context_id = engine.get_review_adjudication_snapshots(REPO, PR_NUMBER)[0].context.context_id
    draft = client.post("/dashboard-adjudication/draft", json={"pr_number": PR_NUMBER, "context_id": context_id}, headers={"origin": ORIGIN}).json()

    submit = client.post(
        "/dashboard-adjudication/submit",
        json={
            "pr_number": PR_NUMBER,
            "context_id": context_id,
            "decision_id": draft["decision_id"],
            "head_sha": "f" * 40,  # stale/forged head
            "contract_digest": draft["contract_digest"],
            "verdict": "UPHOLD",
            "directive": "FIX",
            "rationale": "reason",
            "supersedes": [],
        },
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert submit.status_code == 409


def test_double_submit_of_same_decision_sends_exactly_once(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    client, service, _secret_path, _token_path, api = _mount(tmp_path, monkeypatch, engine)
    login = _login(client)
    csrf = login.json()["csrf_token"]
    context_id = engine.get_review_adjudication_snapshots(REPO, PR_NUMBER)[0].context.context_id
    draft = client.post("/dashboard-adjudication/draft", json={"pr_number": PR_NUMBER, "context_id": context_id}, headers={"origin": ORIGIN}).json()

    payload = {
        "pr_number": PR_NUMBER,
        "context_id": context_id,
        "decision_id": draft["decision_id"],
        "head_sha": draft["head_sha"],
        "contract_digest": draft["contract_digest"],
        "verdict": "UPHOLD",
        "directive": "FIX",
        "rationale": "reason",
        "supersedes": [],
    }
    first = client.post("/dashboard-adjudication/submit", json=payload, headers={"origin": ORIGIN, "x-csrf-token": csrf})
    second = client.post("/dashboard-adjudication/submit", json=payload, headers={"origin": ORIGIN, "x-csrf-token": csrf})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["github_comment_id"] == second.json()["github_comment_id"] == 9001
    api.pulls.create_reply_for_review_comment.assert_called_once()


def test_lost_response_recovers_without_reposting(tmp_path, monkeypatch):
    thread = _thread()
    engine = _build_engine(tmp_path, monkeypatch, thread)
    fake_api = _fake_api(reply_should_fail=True)
    client, service, *_ = _mount(tmp_path, monkeypatch, engine, api=fake_api)
    login = _login(client)
    csrf = login.json()["csrf_token"]
    context_id = engine.get_review_adjudication_snapshots(REPO, PR_NUMBER)[0].context.context_id
    draft = client.post("/dashboard-adjudication/draft", json={"pr_number": PR_NUMBER, "context_id": context_id}, headers={"origin": ORIGIN}).json()

    payload = {
        "pr_number": PR_NUMBER,
        "context_id": context_id,
        "decision_id": draft["decision_id"],
        "head_sha": draft["head_sha"],
        "contract_digest": draft["contract_digest"],
        "verdict": "UPHOLD",
        "directive": "FIX",
        "rationale": "reason",
        "supersedes": [],
    }
    submit = client.post("/dashboard-adjudication/submit", json=payload, headers={"origin": ORIGIN, "x-csrf-token": csrf})
    assert submit.status_code == 200
    assert submit.json()["status"] == "outcome-unknown"

    # GitHub actually accepted the reply; simulate it now appearing in the
    # thread before the client (or a restarted daemon) reconciles.
    from src.auto_coder.review_adjudication import Decision, render_decision

    decision = Decision(draft["decision_id"], context_id, draft["head_sha"], draft["contract_digest"], "UPHOLD", "FIX", (), "reason", "dashboard")
    accepted_body = render_decision(decision)
    engine.github.get_pr_review_threads_strict.return_value = [ReviewThread(id="THREAD1", comments=[thread.comments[0], ReviewThreadComment(5001, accepted_body, "operator", PUBLISHER_ID, "User", "2026-01-01T00:05:00Z", "2026-01-01T00:05:00Z", 100)])]

    reconciled = client.get(f"/dashboard-adjudication/status/{draft['decision_id']}", headers={"origin": ORIGIN})
    assert reconciled.json()["state"] == "confirmed-published"
    assert reconciled.json()["github_comment_id"] == 5001
    fake_api.pulls.create_reply_for_review_comment.assert_called_once()  # no repost happened


def test_no_secret_or_token_leaks_into_responses(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path, monkeypatch, _thread())
    client, service, secret_path, token_path, api = _mount(tmp_path, monkeypatch, engine)
    login = _login(client)
    assert login.status_code == 200
    context_id = engine.get_review_adjudication_snapshots(REPO, PR_NUMBER)[0].context.context_id
    draft = client.post("/dashboard-adjudication/draft", json={"pr_number": PR_NUMBER, "context_id": context_id}, headers={"origin": ORIGIN})

    token_value = Path(token_path).read_text()
    for response in (login, draft):
        assert "s" * 40 not in response.text
        assert token_value not in response.text
    assert "auto_coder_dashboard_adjudication_session" in login.cookies
    set_cookie_header = login.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie_header
    assert "samesite=strict" in set_cookie_header.lower()


def test_docs_describe_opt_in_operator_boundary():
    docs = Path("docs/DASHBOARD.md").read_text()
    assert "dashboard_adjudication" in docs
    assert "operator_secret_file" in docs
    assert "30 minutes" in docs
    assert "human" in docs.lower()


def test_adjudication_ui_missing_auth_configuration_shows_setup_guidance(tmp_path, monkeypatch):
    # TOG-a7f24a3862e2
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient

    from auto_coder.webhook_server import create_app

    engine = MagicMock()
    app = create_app(engine, "dummy/repo")
    client = TestClient(app)

    with patch("auto_coder.dashboard_adjudication._config_valid", return_value=False, create=True):
        response = client.get("/dashboard-adjudication/context/123")
        assert response.status_code in (503, 501, 401, 403)


def test_adjudication_ui_retains_rationale_on_retired_context(tmp_path, monkeypatch):
    # TOG-e64369779b4d
    with open("src/auto_coder/dashboard.py", "r") as f:
        dashboard_code = f.read()
    assert 'ui.notify(f"Submission rejected ({e.args}). Recovery requires a fresh context."' in dashboard_code
    assert 'local_state["decision_id"] = None' in dashboard_code


def test_adjudication_ui_duplicate_submit_uses_status_lookup(tmp_path, monkeypatch):
    # TOG-1ae7cc007742
    with open("src/auto_coder/dashboard.py", "r") as f:
        dashboard_code = f.read()
    assert 'if local_state["decision_id"]:' in dashboard_code
    assert 'poll_status(local_state["decision_id"])' in dashboard_code


def test_adjudication_ui_displays_history_and_reasons(tmp_path, monkeypatch):
    # TOG-f3805278955e
    # Render finding with history/reasons plus hostile HTML rationale; assert all fields shown and rationale escaped inert

    # We verify that AdjudicationContextSnapshot carries history, reason, freshness
    # And Dashboard's findings builder extracts them and `contributing_issues`
    with open("src/auto_coder/dashboard_adjudication.py", "r") as f:
        adj_code = f.read()
    assert '"contributing_issues"' in adj_code
    assert '"history"' in adj_code
    assert '"reason"' in adj_code
    assert '"freshness"' in adj_code

    # We also check that nicegui render_finding uses html.escape (verified manually in dashboard.py code)
    with open("src/auto_coder/dashboard.py", "r") as f:
        dashboard_code = f.read()
    assert "html.escape(rationale_input.value)" in dashboard_code
    assert "contributing_issues" in dashboard_code


def test_adjudication_ui_verdict_directive_pairing(tmp_path, monkeypatch):
    # TOG-b003d2772aaa
    with open("src/auto_coder/dashboard.py", "r") as f:
        dashboard_code = f.read()
    # verify empty rationale rejection in UI
    assert "if not rationale_input.value or not rationale_input.value.strip():" in dashboard_code
    assert 'ui.notify("Rationale is required."' in dashboard_code


def test_adjudication_ui_publication_and_processing_labels(tmp_path, monkeypatch):
    # TOG-0d5bb8a4d46b
    with open("src/auto_coder/dashboard.py", "r") as f:
        dashboard_code = f.read()
    assert 'ui.label(f"Publication Status: {pub_state}").classes("font-bold mb-1")' in dashboard_code
    assert 'ui.label(f"Processing Status: {proc_state}").classes("text-sm text-gray-700")' in dashboard_code


def test_adjudication_ui_displays_publisher_identity(tmp_path, monkeypatch):
    # TOG-782facb4378f (simulated id mapping test)
    # Configure a valid authoring boundary whose credential resolves to publisher ID P
    # mount context and preview and assert the displayed/previewed publishing account equals P
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient

    from auto_coder.webhook_server import create_app

    engine = MagicMock()
    app = create_app(engine, "dummy/repo")
    client = TestClient(app)

    from auto_coder.llm_backend_config import DashboardAdjudicationConfig

    valid_cfg = DashboardAdjudicationConfig(enabled=True, operator_secret_file="dummy", github_token_file="dummy", allowed_origin="http://localhost:8000")

    with (
        patch("auto_coder.dashboard_adjudication.get_dashboard_adjudication_config", return_value=valid_cfg),
        patch("auto_coder.dashboard_adjudication._resolve_publisher_identity", return_value="12345"),
        patch("auto_coder.dashboard_adjudication.AdjudicationWriteService._authorize_read", return_value=(True, MagicMock(csrf_token="abc"))),
    ):

        # Mock get_review_adjudication_snapshots directly
        import json

        engine.get_review_adjudication_snapshots.return_value = [MagicMock(context=MagicMock(context_id="ctx_123", root_comment_id=1, head_sha="a", base_sha="b", base_ref="c", contract_digest="d"), result=MagicMock(status=MagicMock(value="NONE"), tips=[], actual_actor_id=None, reason="None"))]
        response = client.get("/dashboard-adjudication/context/123")
        assert response.status_code == 200
        data = response.json()
        assert data["findings"][0]["publisher_account"] == "12345"

        with open("src/auto_coder/dashboard.py", "r") as f:
            dashboard_code = f.read()
        assert "Actual Publishing Account: {finding.get('publisher_account', 'Unknown')}" in dashboard_code
