from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager
from auto_coder.issue_processor import (
    _process_issue_claude_routine_mode,
    _process_issue_cloud_backend,
    _process_issue_high_score_cloud,
    _process_issue_jules_mode,
    _take_issue_actions,
)
from auto_coder.issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore
from auto_coder.retry_dispatch import RetryDispatchRepository


def owned_authority(issue_number: int = 41, request_id: str = "request-routes", attempt_id: str = "attempt-routes") -> ImplementationRetryRequest:
    del attempt_id
    routing = IssueStageRoutingStore(Path.home() / ".auto-coder" / "issue-stage-routing.sqlite3")
    existing = routing.retry_request(request_id)
    if existing is not None:
        return existing
    routing.accept_retry_request(request_id, "owner/repo", issue_number, "generation-routes")
    predecessor = CloudManager("owner/repo").read_bindings_strict().get(str(issue_number))
    routing.capture_retry_predecessor(
        request_id,
        predecessor.provider if predecessor else None,
        predecessor.task_id if predecessor else None,
        predecessor.backend_name if predecessor else None,
    )
    return routing.mark_retry_owned(request_id, f"invocation-{request_id}")


def issue() -> dict:
    return {"number": 41, "title": "Retry all routes", "body": "Implement it", "labels": []}


def test_local_retry_claims_before_real_invocation_and_completed_replay_does_not_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    github = MagicMock()
    github.get_all_sub_issues.return_value = []
    github.get_parent_issue_details.return_value = None
    github.get_open_sub_issues.return_value = []
    backend = MagicMock(backend_name="local-alias")

    with patch("auto_coder.issue_processor._apply_issue_actions_directly", return_value=["implemented"]) as invoke:
        first = _take_issue_actions("owner/repo", issue(), AutomationConfig(), github, backend_manager=backend, retry_authority=owned_authority())
        replay = _take_issue_actions("owner/repo", issue(), AutomationConfig(), github, backend_manager=backend, retry_authority=owned_authority())

    assert first == ["implemented"]
    assert replay == ["implemented"]
    invoke.assert_called_once()
    handoff = RetryDispatchRepository("owner/repo").get("request-routes")
    assert handoff is not None
    assert (handoff.route, handoff.backend_name, handoff.outcome, handoff.external_id) == (
        "local",
        "local-alias",
        "completed",
        "invocation-request-routes",
    )


@pytest.mark.parametrize("route", ["jules", "claude-routine"])
def test_remote_retry_alias_claims_once_and_replay_repairs_same_receipt(route, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    github = MagicMock()
    manager = CloudManager("owner/repo")
    config = AutomationConfig()
    authority = owned_authority()

    if route == "jules":
        client = MagicMock()
        client.start_session.return_value = "session-routes"
        with patch("auto_coder.issue_processor.JulesClient", return_value=client), patch("auto_coder.issue_processor.get_commit_log", return_value=""):
            first = _process_issue_jules_mode("owner/repo", issue(), config, github, backend_name="jules-alias", retry_authority=authority)
            replay = _process_issue_jules_mode("owner/repo", issue(), config, github, backend_name="jules-alias", retry_authority=authority)
        create = client.start_session
    else:
        client = MagicMock()
        client.fire_routine.return_value = ("session-routes", "https://example.test/session-routes")
        with patch("auto_coder.claude_routine_client.ClaudeRoutineClient", return_value=client), patch("auto_coder.issue_processor.get_commit_log", return_value=""):
            first = _process_issue_claude_routine_mode("owner/repo", issue(), config, github, backend_name="routine-alias", retry_authority=authority)
            replay = _process_issue_claude_routine_mode("owner/repo", issue(), config, github, backend_name="routine-alias", retry_authority=authority)
        create = client.fire_routine

    assert "session-routes" in first[-1]
    assert "already accepted" in replay[0]
    create.assert_called_once()
    assert CloudManager("owner/repo").get_binding(41).task_id == "session-routes"


@pytest.mark.parametrize("selector", [_process_issue_cloud_backend, _process_issue_high_score_cloud])
@pytest.mark.parametrize(
    ("backend_type", "adapter"),
    [
        ("jules", "_process_issue_jules_mode"),
        ("claude-routine", "_process_issue_claude_routine_mode"),
        ("codex-cloud", "_process_issue_codex_cloud_mode"),
    ],
)
def test_cloud_selectors_forward_owned_authority_and_named_alias(selector, backend_type, adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = owned_authority()
    llm_config = MagicMock()
    llm_config.backend_cloud_priority_groups = []
    llm_config.backend_cloud_order = ["named-alias"]
    llm_config.get_backend_cloud.return_value = None
    llm_config.backend_with_high_score_cloud_order = ["named-alias"]
    llm_config.get_backend_with_high_score_cloud.return_value = None
    llm_config.get_backend_config.return_value.backend_type = backend_type

    with (
        patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config),
        patch("auto_coder.quota_selector.rank_high_score_backends_by_quota", return_value=["named-alias"]),
        patch(f"auto_coder.issue_processor.{adapter}", return_value=["selected"]) as dispatch,
    ):
        assert selector("owner/repo", issue(), AutomationConfig(), MagicMock(), retry_authority=authority) == ["selected"]

    assert dispatch.call_args.kwargs["backend_name"] == "named-alias"
    assert dispatch.call_args.kwargs["retry_authority"] == authority


def test_missing_or_invalid_authority_refuses_every_creation_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    invalid = ImplementationRetryRequest(
        request_id="invalid",
        repository="owner/repo",
        target_number=41,
        generation="generation-routes",
        attempt_id="attempt-invalid",
        status="invalidated",
    )
    github = MagicMock()
    github.get_all_sub_issues.return_value = []
    github.get_parent_issue_details.return_value = None
    github.get_open_sub_issues.return_value = []

    with patch("auto_coder.issue_processor._apply_issue_actions_directly") as local, patch("auto_coder.issue_processor.JulesClient") as jules_type, patch("auto_coder.claude_routine_client.ClaudeRoutineClient") as routine_type:
        local_result = _take_issue_actions("owner/repo", issue(), AutomationConfig(), github, retry_authority=invalid)
        jules_result = _process_issue_jules_mode("owner/repo", issue(), AutomationConfig(), github, retry_authority=invalid)
        with pytest.raises(Exception, match="has not acquired"):
            _process_issue_claude_routine_mode("owner/repo", issue(), AutomationConfig(), github, retry_authority=invalid)

    assert "authority is unavailable" in local_result[0]
    assert "has not acquired" in jules_result[0]
    local.assert_not_called()
    jules_type.return_value.start_session.assert_not_called()
    routine_type.return_value.fire_routine.assert_not_called()


@pytest.mark.parametrize("selector", [_process_issue_cloud_backend, _process_issue_high_score_cloud])
def test_boolean_only_retry_cannot_reach_any_selected_provider(selector):
    with patch("auto_coder.llm_backend_config.get_llm_config") as config_read:
        result = selector("owner/repo", issue(), AutomationConfig(), MagicMock(), manual_retry=True)

    assert result == ["Deferred cloud retry for issue #41: durable retry authority is required"]
    config_read.assert_not_called()


@pytest.mark.parametrize("route", ["jules", "claude-routine"])
def test_stale_retry_replay_does_not_replace_newer_accepted_pointer(route, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    github = MagicMock()
    config = AutomationConfig()
    first_authority = owned_authority(request_id="request-1", attempt_id="attempt-1")
    if route == "jules":
        client = MagicMock()
        client.start_session.side_effect = ["session-1", "session-2"]
        with patch("auto_coder.issue_processor.JulesClient", return_value=client), patch("auto_coder.issue_processor.get_commit_log", return_value=""):
            _process_issue_jules_mode("owner/repo", issue(), config, github, retry_authority=first_authority)
            second_authority = owned_authority(request_id="request-2", attempt_id="attempt-2")
            _process_issue_jules_mode("owner/repo", issue(), config, github, retry_authority=second_authority)
            replay = _process_issue_jules_mode("owner/repo", issue(), config, github, retry_authority=first_authority)
        create = client.start_session
    else:
        client = MagicMock()
        client.fire_routine.side_effect = [("session-1", "url-1"), ("session-2", "url-2")]
        with patch("auto_coder.claude_routine_client.ClaudeRoutineClient", return_value=client), patch("auto_coder.issue_processor.get_commit_log", return_value=""):
            _process_issue_claude_routine_mode("owner/repo", issue(), config, github, retry_authority=first_authority)
            second_authority = owned_authority(request_id="request-2", attempt_id="attempt-2")
            _process_issue_claude_routine_mode("owner/repo", issue(), config, github, retry_authority=second_authority)
            replay = _process_issue_claude_routine_mode("owner/repo", issue(), config, github, retry_authority=first_authority)
        create = client.fire_routine

    assert CloudManager("owner/repo").get_binding(41).task_id == "session-2"
    assert "historical" in replay[0]
    assert "newer accepted retry remains current" in replay[0]
    assert RetryDispatchRepository("owner/repo").get("request-1").external_id == "session-1"
    assert RetryDispatchRepository("owner/repo").get("request-2").external_id == "session-2"
    assert create.call_count == 2
