"""Tests for delegating merge-conflict repair to originating cloud sessions."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.cloud_run import CloudRun
from src.auto_coder.cloud_task_client_base import CloudTaskClientBase
from src.auto_coder.issue_processor import _process_issue_claude_routine_mode
from src.auto_coder.llm_backend_config import BackendConfig
from src.auto_coder.pr_processor import (
    _delegate_cloud_merge_conflict_repair,
    _delegate_cloud_merge_conflict_repair_result,
    _delegate_cloud_review_thread_repair,
    _link_jules_pr_to_issue,
    _merge_pr,
    _record_cloud_conflict_deliveries,
    _resolve_cloud_conflict_origin,
    _update_with_base_branch,
)
from src.auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment
from src.auto_coder.utils import CommandResult


class FollowupClient:
    """Small capability provider used to observe follow-up delivery."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.messages: list[tuple[str, str]] = []

    def send_followup(self, task_id: str, message: str) -> bool:
        self.messages.append((task_id, message))
        return self.accepted


class UnsupportedClient:
    """Provider that retains the optional base implementation."""

    send_followup = CloudTaskClientBase.send_followup


def pr_data(head_sha: str = "H1", base_sha: str = "B1") -> dict:
    """Build complete PR metadata for a release-branch conflict."""
    return {
        "number": 1589,
        "body": "Closes #1589",
        "head": {"ref": "cloud/repair-1589", "sha": head_sha},
        "base": {"ref": "release/2.x", "sha": base_sha},
    }


@pytest.mark.parametrize(
    "body",
    ["Closes #1748", "Created by Claude: https://claude.ai/code/session-a"],
    ids=["linked-issue", "session-url"],
)
def test_named_claude_dispatch_survives_restart_through_conflict_delivery(tmp_path, monkeypatch, body) -> None:
    """Production persistence and conflict delivery retain backend credentials."""
    backend_a = BackendConfig(
        name="claude-a",
        backend_type="claude-routine",
        url="https://claude-a.example/fire",
        api_key="token-a",
    )
    backend_b = BackendConfig(
        name="claude-routine",
        backend_type="claude-routine",
        url="https://claude-b.example/fire",
        api_key="token-b",
    )
    llm_config = MagicMock()
    llm_config.get_backend_config.side_effect = {"claude-a": backend_a, "claude-routine": backend_b}.get
    github = MagicMock()
    issue = {"number": 1748, "title": "Preserve backend", "body": "Details", "labels": [], "state": "open"}
    pull_request = pr_data()
    pull_request["body"] = body
    pull_request["user"] = {"login": "claude[bot]"}
    monkeypatch.setenv("CLAUDE_CODE_ROUTINE_TOKEN", "token-b")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token-b")

    with (
        patch("src.auto_coder.cloud_manager.Path.home", return_value=tmp_path),
        patch("src.auto_coder.pr_processor.Path.home", return_value=tmp_path),
        patch("src.auto_coder.claude_routine_client.get_llm_config", return_value=llm_config),
        patch("src.auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine", return_value=("session-a", None)),
        patch("src.auto_coder.issue_processor.get_commit_log", return_value="initial"),
        patch("src.auto_coder.claude_routine_client.CommandExecutor.run_command", return_value=CommandResult(True, "", "", 0)) as command,
    ):
        _process_issue_claude_routine_mode("owner/repo", issue, AutomationConfig(), github, backend_name="claude-a")
        result = _delegate_cloud_merge_conflict_repair_result("owner/repo", pull_request, github)

    assert result.delegated is True
    args, kwargs = command.call_args
    assert args[0][2] == "--cloud=session-a"
    assert kwargs["env"]["CLAUDE_CODE_ROUTINE_TOKEN"] == "token-a"
    assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "token-a"


def test_duplicate_named_backend_session_url_fails_closed_after_dispatch(tmp_path) -> None:
    """A reverse session lookup cannot arbitrarily choose the first CSV issue."""
    backends = {
        name: BackendConfig(
            name=name,
            backend_type="claude-routine",
            url=f"https://{name}.example/fire",
            api_key=f"token-{name}",
        )
        for name in ("claude-a", "claude-b")
    }
    llm_config = MagicMock()
    llm_config.get_backend_config.side_effect = backends.get
    response = MagicMock(status_code=201, text="")
    response.json.return_value = {"claude_code_session_id": "shared-session"}
    github = MagicMock()

    with (
        patch("src.auto_coder.cloud_manager.Path.home", return_value=tmp_path),
        patch("src.auto_coder.pr_processor.Path.home", return_value=tmp_path),
        patch("src.auto_coder.claude_routine_client.get_llm_config", return_value=llm_config),
        patch("src.auto_coder.claude_routine_client.check_claude_usage_or_raise"),
        patch("src.auto_coder.claude_routine_client.requests.Session.post", return_value=response),
        patch("src.auto_coder.claude_routine_client._save_claude_routine_state"),
        patch("src.auto_coder.issue_processor.get_commit_log", return_value="initial"),
        patch("src.auto_coder.claude_routine_client.CommandExecutor.run_command") as command,
    ):
        for issue_number, backend_name in ((1, "claude-b"), (2, "claude-a")):
            issue = {"number": issue_number, "title": "Shared session", "body": "Details", "labels": [], "state": "open"}
            _process_issue_claude_routine_mode("owner/repo", issue, AutomationConfig(), github, backend_name=backend_name)

        pull_request = pr_data()
        pull_request["body"] = "Created by Claude: https://claude.ai/code/shared-session"
        pull_request["user"] = {"login": "claude[bot]"}
        original_body = pull_request["body"]
        github.search_issues.return_value = []

        linked = _link_jules_pr_to_issue("owner/repo", pull_request, github)
        conflict_result = _delegate_cloud_merge_conflict_repair_result("owner/repo", pull_request, github)
        review_result = _delegate_cloud_review_thread_repair(
            "owner/repo",
            pull_request,
            github,
            (ReviewThread(id="thread-1", comments=[ReviewThreadComment(database_id=1, body="Fix this")]),),
        )

    assert linked is False
    assert pull_request["body"] == original_body
    github.update_pr_body.assert_not_called()
    assert conflict_result.delegated is False
    assert conflict_result.reason == "no originating cloud implementation session could be resolved"
    assert review_result.delivered is False
    assert review_result == ["Review repair was not delivered for PR #1589: no provider-owned cloud task association was found"]
    command.assert_not_called()


def test_delegation_uses_existing_task_and_actual_pr_branches(tmp_path) -> None:
    client = FollowupClient()
    state_path = tmp_path / "repairs.json"

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())

    assert len(client.messages) == 1
    task_id, message = client.messages[0]
    assert task_id == "task_existing"
    assert "release/2.x" in message
    assert "cloud/repair-1589" in message
    assert "head branch `cloud/repair-1589`" in message
    assert "H1" in message
    assert "existing pull request #1589" in message
    assert "Do not create a new branch." in message
    assert "Do not create a new pull request." in message
    assert "Do not replace or close the existing pull request." in message


def test_production_conflict_path_reports_confirmed_followup_in_actions_and_pr(tmp_path) -> None:
    """The supported merge path preserves provider acceptance through both outputs."""
    client = FollowupClient()
    github = Mock()
    github.get_pr_comments.return_value = []
    commands = [
        CommandResult(True, "", "", 0),
        CommandResult(True, "3\n", "", 0),
        CommandResult(False, "CONFLICT", "", 1),
        CommandResult(True, "", "", 0),
    ]

    with (
        patch("src.auto_coder.pr_processor.cmd") as command_executor,
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_e_accepted")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "repairs.json"),
        patch("src.auto_coder.pr_processor._is_local_llm_pr", return_value=False),
    ):
        command_executor.run_command.side_effect = commands
        actions = _update_with_base_branch("owner/repo", pr_data(), AutomationConfig(), github)

    assert "Codex Cloud task 'task_e_accepted' accepted merge-conflict-repair follow-up for PR #1589 at head H1" in actions
    assert "Delegated merge-conflict repair for PR #1589 to its existing cloud session" in actions
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    comment = github.add_comment_to_pr.call_args.args[2]
    assert "task_e_accepted" in comment
    assert "PR #1589" in comment
    assert "head `H1`" in comment
    assert "merge-conflict-repair" in comment


def test_direct_merge_path_propagates_confirmed_followup_to_action_sink(tmp_path) -> None:
    """A failed API merge must expose acceptance before deferring cloud repair."""
    cloud_client = FollowupClient()
    github = Mock(token="token")
    github.get_pr_comments.return_value = []
    api = Mock()
    api.pulls.merge.return_value = {"merged": False}
    conflicting_pr = pr_data()
    conflicting_pr["mergeable"] = False
    api.pulls.get.return_value = conflicting_pr

    with (
        patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=api),
        patch("src.auto_coder.pr_processor._get_review_thread_gate_state", return_value=Mock(lookup_error=None, has_unresolved=False)),
        patch("src.auto_coder.pr_processor._get_allowed_merge_methods", return_value=[]),
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(cloud_client, "task_e_direct")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "repairs.json"),
        patch("src.auto_coder.pr_processor._resolve_pr_merge_conflicts") as local_resolution,
        patch("src.auto_coder.pr_processor.log_action") as action_sink,
    ):
        merged = _merge_pr("owner/repo", 1589, {}, AutomationConfig(), github)

    assert merged is False
    assert len(cloud_client.messages) == 1
    action_sink.assert_any_call("Codex Cloud task 'task_e_direct' accepted merge-conflict-repair follow-up for PR #1589 at head H1")
    action_sink.assert_any_call("Delegated merge-conflict repair for PR #1589 to its existing cloud session")
    local_resolution.assert_not_called()


def test_delegation_rejects_invalid_metadata_and_uses_provider_task_from_pr_body(tmp_path) -> None:
    """Exercise conflict delegation through its real task-resolution boundary."""
    client = FollowupClient()
    state_path = tmp_path / "repairs.json"
    pull_request = pr_data()
    pull_request["_codex_task_id"] = "task_fake"
    pull_request["body"] = "Closes #1589\n\nhttps://chatgpt.com/codex/tasks/task_e_real123"

    with (
        patch("src.auto_coder.codex_cloud_client.CodexCloudClient", return_value=client),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pull_request) is True

    assert len(client.messages) == 1
    task_id, message = client.messages[0]
    assert task_id == "task_e_real123"
    assert "task_fake" not in message
    assert state_path.exists()


def test_unchanged_state_is_deduplicated_but_changed_states_can_delegate(tmp_path) -> None:
    client = FollowupClient()
    state_path = tmp_path / "repairs.json"

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data(head_sha="H2"))
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data(head_sha="H2", base_sha="B2"))

    assert [task_id for task_id, _ in client.messages] == ["task_existing"] * 3


def test_comment_receipt_is_deduplicated_per_head_and_purpose(tmp_path) -> None:
    client = FollowupClient()
    github = Mock()
    comments: list[dict] = []
    github.get_pr_comments.side_effect = lambda *_args: list(comments)
    github.add_comment_to_pr.side_effect = lambda _repo, _number, body: comments.append({"body": body})

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "repairs.json"),
    ):
        first = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data(), github)
        repeated = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data(), github)
        advanced = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data(head_sha="H2"), github)

    assert first.accepted_action
    assert repeated.accepted_action
    assert advanced.accepted_action.endswith("at head H2")
    assert len(client.messages) == 2
    assert github.add_comment_to_pr.call_count == 2
    assert "head `H1`" in comments[0]["body"]
    assert "head `H2`" in comments[1]["body"]


def test_comment_failure_retries_publication_without_resending_followup(tmp_path) -> None:
    client = FollowupClient()
    github = Mock()
    github.get_pr_comments.return_value = []
    github.add_comment_to_pr.side_effect = [RuntimeError("GitHub unavailable"), None]

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "repairs.json"),
    ):
        first = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data(), github)
        second = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data(), github)

    assert first.accepted_action == second.accepted_action
    assert len(client.messages) == 1
    assert github.add_comment_to_pr.call_count == 2


def test_failed_delivery_and_unsupported_provider_preserve_fallback(tmp_path) -> None:
    state_path = tmp_path / "repairs.json"
    failed_client = FollowupClient(accepted=False)

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(failed_client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        result = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data())

    assert not result
    assert result.reason == "delivery to originating cloud session 'task_existing' was rejected"

    assert len(failed_client.messages) == 1
    assert state_path.read_text(encoding="utf-8") == "{}"

    unsupported = object.__new__(UnsupportedClient)
    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(unsupported, "session_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        result = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data())

    assert not result
    assert "does not support repair follow-up" in result.reason


def test_missing_origin_preserves_fallback_without_creating_a_task() -> None:
    with patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=None) as resolver:
        result = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data())

    resolver.assert_called_once()
    assert not result
    assert result.reason == "no originating cloud implementation session could be resolved"


def test_delivery_is_reserved_before_followup_and_never_redelivered(tmp_path) -> None:
    """A confirmed send transitions its durable reservation before deduplication."""
    client = FollowupClient()
    state_path = tmp_path / "repairs.json"

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
        patch("src.auto_coder.pr_processor._record_cloud_conflict_deliveries", wraps=_record_cloud_conflict_deliveries) as record,
    ):
        first = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data())
        second = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data())

    assert first
    assert second
    assert len(client.messages) == 1
    assert record.call_count == 2


def test_reservation_failure_prevents_non_idempotent_delivery(tmp_path) -> None:
    """Never send when the durable unchanged-state guard cannot be written."""
    client = FollowupClient()

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "repairs.json"),
        patch("src.auto_coder.pr_processor._record_cloud_conflict_deliveries", side_effect=OSError("read-only filesystem")),
    ):
        result = _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data())

    assert not result
    assert result.reason == "a durable repair delivery receipt could not be reserved: read-only filesystem"
    assert client.messages == []


def test_rejected_delivery_with_failed_cleanup_is_not_reported_as_delegated(tmp_path) -> None:
    """Production remediation treats a stranded reservation as unconfirmed."""
    client = FollowupClient(accepted=False)
    state_path = tmp_path / "repairs.json"
    write_count = 0
    commands = [
        CommandResult(True, "", "", 0),
        CommandResult(True, "3\n", "", 0),
        CommandResult(False, "CONFLICT", "", 1),
        CommandResult(True, "", "", 0),
    ] * 2

    def fail_cleanup(path, deliveries) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("cleanup unavailable")
        _record_cloud_conflict_deliveries(path, deliveries)

    with (
        patch("src.auto_coder.pr_processor.cmd") as command_executor,
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
        patch("src.auto_coder.pr_processor._record_cloud_conflict_deliveries", side_effect=fail_cleanup),
        patch("src.auto_coder.pr_processor._is_local_llm_pr", return_value=False),
    ):
        command_executor.run_command.side_effect = commands
        first = _update_with_base_branch("owner/repo", pr_data(), AutomationConfig())
        second = _update_with_base_branch("owner/repo", pr_data(), AutomationConfig())

    assert any("was rejected" in action and "reservation could not be cleared" in action for action in first)
    assert any("has unconfirmed status" in action and "not resending" in action for action in second)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in first
    assert "ACTION_FLAG:SKIP_ANALYSIS" in second
    assert not any("Delegated merge-conflict repair" in action for action in first + second)
    assert len(client.messages) == 1


def test_production_conflict_path_reports_rejected_cloud_delivery(tmp_path) -> None:
    """A resolved session's rejection is reported instead of misclassified."""
    client = FollowupClient(accepted=False)
    state_path = tmp_path / "repairs.json"
    commands = [
        CommandResult(True, "", "", 0),
        CommandResult(True, "3\n", "", 0),
        CommandResult(False, "CONFLICT", "", 1),
        CommandResult(True, "", "", 0),
    ]

    with (
        patch("src.auto_coder.pr_processor.cmd") as command_executor,
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
        patch("src.auto_coder.pr_processor._is_local_llm_pr", return_value=False),
    ):
        command_executor.run_command.side_effect = commands
        actions = _update_with_base_branch("owner/repo", pr_data(), AutomationConfig())

    assert len(client.messages) == 1
    assert any("delivery to originating cloud session 'task_existing' was rejected" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    assert not any("no usable cloud conflict-repair session" in action for action in actions)
    assert not any("remediation completed" in action.lower() for action in actions)


def test_authoritative_cloud_run_association_precedes_pr_author_heuristics() -> None:
    """A lifecycle-associated PR remains cloud-owned without author markers."""
    run = CloudRun(
        repo_name="owner/repo",
        issue_number=1589,
        attempt=2,
        provider="codex-cloud",
        task_id="task_e_authoritative",
        pull_request_numbers=[1589],
    )
    unmarked_pr = pr_data()
    unmarked_pr["body"] = "Closes #1589"
    unmarked_pr["user"] = {"login": "ordinary-user"}

    with (
        patch("src.auto_coder.cloud_run.CloudRunRepository.list_for_issue", return_value=[run]),
        patch("src.auto_coder.codex_cloud_client.CodexCloudClient") as client_type,
    ):
        origin = _resolve_cloud_conflict_origin("owner/repo", unmarked_pr)

    assert origin is not None
    client, task_id = origin
    assert client is client_type.return_value
    assert task_id == "task_e_authoritative"
    client_type.assert_called_once_with(repo_name="owner/repo")
