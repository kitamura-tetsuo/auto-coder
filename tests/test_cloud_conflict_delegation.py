"""Tests for delegating merge-conflict repair to originating cloud sessions."""

from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.cloud_run import CloudRun
from src.auto_coder.cloud_task_client_base import CloudTaskClientBase
from src.auto_coder.pr_processor import (
    _delegate_cloud_merge_conflict_repair,
    _record_cloud_conflict_deliveries,
    _resolve_cloud_conflict_origin,
    _update_with_base_branch,
)
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


def test_failed_delivery_and_unsupported_provider_preserve_fallback(tmp_path) -> None:
    state_path = tmp_path / "repairs.json"
    failed_client = FollowupClient(accepted=False)

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(failed_client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        result = _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())

    assert not result
    assert result.reason == "delivery to originating cloud session 'task_existing' was rejected"

    assert len(failed_client.messages) == 1
    assert state_path.read_text(encoding="utf-8") == "{}"

    unsupported = object.__new__(UnsupportedClient)
    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(unsupported, "session_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        result = _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())

    assert not result
    assert "does not support repair follow-up" in result.reason


def test_missing_origin_preserves_fallback_without_creating_a_task() -> None:
    with patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=None) as resolver:
        result = _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())

    resolver.assert_called_once()
    assert not result
    assert result.reason == "no originating cloud implementation session could be resolved"


def test_delivery_is_reserved_before_followup_and_never_redelivered(tmp_path) -> None:
    """A confirmed send has no fallible receipt write after delivery."""
    client = FollowupClient()
    state_path = tmp_path / "repairs.json"

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
        patch("src.auto_coder.pr_processor._record_cloud_conflict_deliveries", wraps=_record_cloud_conflict_deliveries) as record,
    ):
        first = _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())
        second = _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())

    assert first
    assert second
    assert len(client.messages) == 1
    record.assert_called_once()


def test_reservation_failure_prevents_non_idempotent_delivery(tmp_path) -> None:
    """Never send when the durable unchanged-state guard cannot be written."""
    client = FollowupClient()

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "repairs.json"),
        patch("src.auto_coder.pr_processor._record_cloud_conflict_deliveries", side_effect=OSError("read-only filesystem")),
    ):
        result = _delegate_cloud_merge_conflict_repair("owner/repo", pr_data())

    assert not result
    assert result.reason == "a durable repair delivery receipt could not be reserved: read-only filesystem"
    assert client.messages == []


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
