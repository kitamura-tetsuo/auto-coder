"""
Unit tests for routing Codex Cloud Issue dispatch through CloudRun (issue #1606).

These tests cover the acceptance scenarios from the issue:
- AC-001: initial Codex dispatch persists a durable CloudRun.
- AC-002: reprocessing the same attempt must not call start_task() again.
- AC-003: a missing @auto-coder label does not authorize duplicate dispatch.
- AC-004: duplicate protection survives a simulated process restart.
- AC-005/AC-006: CodexCloudRunPolicy allows only explicit manual retries.
"""

from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_run import CloudRun, CloudRunEvent, CloudRunRepository
from auto_coder.cloud_run_policies import MANUAL_RETRY_REASON, CodexCloudRunPolicy
from auto_coder.issue_processor import _process_issue_codex_cloud_mode


def _issue_data(number: int = 100) -> dict:
    return {"number": number, "title": "Fix it", "body": "Please fix", "labels": []}


class TestCodexCloudDispatchDuplicateProtection:
    """AC-001, AC-002, AC-003: durable CloudRun guards Codex Cloud dispatch."""

    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_initial_dispatch_persists_cloud_run(self, mock_client_type, mock_cloud_manager_type, tmp_path, monkeypatch):
        """AC-001: exactly one task is started and durably associated with the attempt."""
        monkeypatch.setenv("HOME", str(tmp_path))
        client = mock_client_type.return_value
        client.start_task.return_value = "task-A"
        client.task_urls = {"task-A": "https://chatgpt.com/codex/tasks/task-A"}
        github_client = MagicMock()

        with patch("auto_coder.issue_processor.get_commit_log", return_value=""), patch("auto_coder.issue_processor.get_current_attempt", return_value=0):
            actions = _process_issue_codex_cloud_mode(
                "owner/repo",
                _issue_data(100),
                AutomationConfig(),
                github_client,
                backend_name="codex-cloud-luna",
            )

        client.start_task.assert_called_once()
        assert actions == ["Started Codex Cloud task 'task-A' for issue #100"]

        run = CloudRunRepository("owner/repo").get(issue_number=100, attempt=0)
        assert run is not None
        assert run.provider == "codex-cloud"
        assert run.task_id == "task-A"

    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_reprocessing_same_attempt_does_not_start_duplicate_task(self, mock_client_type, mock_cloud_manager_type, tmp_path, monkeypatch):
        """AC-002: task-A remains the run for attempt 0; start_task() is not called again."""
        monkeypatch.setenv("HOME", str(tmp_path))
        CloudRunRepository("owner/repo").save(CloudRun(repo_name="owner/repo", issue_number=100, attempt=0, provider="codex-cloud", task_id="task-A"))
        client = mock_client_type.return_value
        github_client = MagicMock()
        label_context = MagicMock()

        with patch("auto_coder.issue_processor.get_commit_log", return_value=""), patch("auto_coder.issue_processor.get_current_attempt", return_value=0):
            actions = _process_issue_codex_cloud_mode(
                "owner/repo",
                _issue_data(100),
                AutomationConfig(),
                github_client,
                backend_name="codex-cloud-luna",
                label_context=label_context,
            )

        client.start_task.assert_not_called()
        github_client.add_comment_to_issue.assert_not_called()
        label_context.keep_label.assert_called_once_with()
        assert actions == ["Codex Cloud task 'task-A' already running for issue #100 attempt 0; skipped duplicate dispatch"]

        run = CloudRunRepository("owner/repo").get(issue_number=100, attempt=0)
        assert run.task_id == "task-A"

    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_missing_label_does_not_authorize_duplicate_dispatch(self, mock_client_type, mock_cloud_manager_type, tmp_path, monkeypatch):
        """AC-003: no replacement task is created solely because @auto-coder is absent.

        The dispatch guard never inspects the issue's labels, so it behaves
        identically whether or not @auto-coder is present.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        CloudRunRepository("owner/repo").save(CloudRun(repo_name="owner/repo", issue_number=100, attempt=0, provider="codex-cloud", task_id="task-A"))
        client = mock_client_type.return_value
        issue_data = _issue_data(100)
        issue_data["labels"] = []  # @auto-coder absent

        with patch("auto_coder.issue_processor.get_commit_log", return_value=""), patch("auto_coder.issue_processor.get_current_attempt", return_value=0):
            actions = _process_issue_codex_cloud_mode(
                "owner/repo",
                issue_data,
                AutomationConfig(),
                MagicMock(),
                backend_name="codex-cloud-luna",
            )

        client.start_task.assert_not_called()
        assert "task-A" in actions[0]

    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_restart_recovery_prevents_new_task_without_in_memory_state(self, mock_client_type, mock_cloud_manager_type, tmp_path, monkeypatch):
        """AC-004: a fresh CloudRunRepository (simulated restart) still blocks a new task."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # A previous process persisted task-A and then exited.
        first_process_repo = CloudRunRepository("owner/repo")
        first_process_repo.save(CloudRun(repo_name="owner/repo", issue_number=100, attempt=0, provider="codex-cloud", task_id="task-A"))
        del first_process_repo

        client = mock_client_type.return_value

        with patch("auto_coder.issue_processor.get_commit_log", return_value=""), patch("auto_coder.issue_processor.get_current_attempt", return_value=0):
            actions = _process_issue_codex_cloud_mode(
                "owner/repo",
                _issue_data(100),
                AutomationConfig(),
                MagicMock(),
                backend_name="codex-cloud-luna",
            )

        client.start_task.assert_not_called()
        assert "task-A" in actions[0]

    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_manual_new_attempt_is_not_blocked_by_old_run(self, mock_client_type, mock_cloud_manager_type, tmp_path, monkeypatch):
        """AC-006: a human-authorized new attempt is not prevented by the old run.

        A manual retry increments the Issue attempt (e.g. via
        attempt_manager.increment_attempt), so the new attempt has no
        CloudRun yet and dispatch proceeds normally.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        CloudRunRepository("owner/repo").save(CloudRun(repo_name="owner/repo", issue_number=100, attempt=0, provider="codex-cloud", task_id="task-A"))
        client = mock_client_type.return_value
        client.start_task.return_value = "task-B"
        client.task_urls = {}

        with patch("auto_coder.issue_processor.get_commit_log", return_value=""), patch("auto_coder.issue_processor.get_current_attempt", return_value=1):
            actions = _process_issue_codex_cloud_mode(
                "owner/repo",
                _issue_data(100),
                AutomationConfig(),
                MagicMock(),
                backend_name="codex-cloud-luna",
            )

        client.start_task.assert_called_once()
        assert actions == ["Started Codex Cloud task 'task-B' for issue #100"]

        # The old run for attempt 0 is untouched; the new attempt owns task-B.
        repo = CloudRunRepository("owner/repo")
        assert repo.get(issue_number=100, attempt=0).task_id == "task-A"
        assert repo.get(issue_number=100, attempt=1).task_id == "task-B"


class TestCodexCloudRunPolicy:
    """AC-005, AC-006: manual-only retry policy."""

    def _run(self) -> CloudRun:
        return CloudRun(repo_name="owner/repo", issue_number=100, attempt=0, provider="codex-cloud", task_id="task-A")

    def test_automatic_reasons_never_allow_a_new_attempt(self):
        policy = CodexCloudRunPolicy()
        for reason in ("failed", "paused", "unknown", "stalled-timeout", "no-pr", "running"):
            event = CloudRunEvent(run=self._run(), reason=reason, proposed_attempt=1)
            assert policy.allow_new_attempt(event) is False

    def test_explicit_manual_reason_allows_a_new_attempt(self):
        policy = CodexCloudRunPolicy()
        event = CloudRunEvent(run=self._run(), reason=MANUAL_RETRY_REASON, proposed_attempt=1)
        assert policy.allow_new_attempt(event) is True
