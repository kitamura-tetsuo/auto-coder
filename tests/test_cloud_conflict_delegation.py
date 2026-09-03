"""Tests for delegating merge-conflict repair to originating cloud sessions."""

from unittest.mock import Mock, patch

from src.auto_coder.cloud_run import CloudRun
from src.auto_coder.cloud_task_client_base import CloudTaskClientBase
from src.auto_coder.pr_processor import _delegate_cloud_merge_conflict_repair, _resolve_cloud_conflict_origin


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
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data()) is True

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
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data()) is True
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data()) is True
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data(head_sha="H2")) is True
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data(head_sha="H2", base_sha="B2")) is True

    assert [task_id for task_id, _ in client.messages] == ["task_existing"] * 3


def test_failed_delivery_and_unsupported_provider_preserve_fallback(tmp_path) -> None:
    state_path = tmp_path / "repairs.json"
    failed_client = FollowupClient(accepted=False)

    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(failed_client, "task_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data()) is False

    assert len(failed_client.messages) == 1
    assert not state_path.exists()

    unsupported = object.__new__(UnsupportedClient)
    with (
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(unsupported, "session_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
    ):
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data()) is False


def test_missing_origin_preserves_fallback_without_creating_a_task() -> None:
    with patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=None) as resolver:
        assert _delegate_cloud_merge_conflict_repair("owner/repo", pr_data()) is False

    resolver.assert_called_once()


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
