"""
Unit tests for CloudTaskEngine orchestration.
"""

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cloud_task_client_base import (
    CloudTask,
    CloudTaskClientBase,
    CloudTaskState,
)
from auto_coder.cloud_task_engine import CloudTaskEngine


class MockCloudTaskClient(CloudTaskClientBase):
    """Mock cloud task client for orchestration testing."""

    def __init__(self, name: str = "mock-client") -> None:
        super().__init__()
        self.name = name
        self.tasks: list[CloudTask] = []
        self.resumed_tasks: list[str] = []

    def continue_if_paused(self, task_id: str) -> bool:
        for t in self.tasks:
            if t.task_id == task_id and t.state == CloudTaskState.PAUSED:
                t.state = CloudTaskState.RUNNING
                self.resumed_tasks.append(task_id)
                return True
        return False

    def start_task(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
    ) -> str:
        tid = f"task_{len(self.tasks) + 1}"
        self.tasks.append(CloudTask(task_id=tid, state=CloudTaskState.RUNNING, title=title, prompt=prompt))
        return tid

    def list_tasks(self, repo_name: Optional[str] = None) -> list[CloudTask]:
        return list(self.tasks)

    def stop_task(self, task_id: str) -> bool:
        for t in self.tasks:
            if t.task_id == task_id:
                t.state = CloudTaskState.FAILED
                return True
        return False

    def _run_llm_cli(self, prompt: str) -> str:
        return "ok"

    def check_mcp_server_configured(self, server_name: str) -> bool:
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        return False


class TestCloudTaskEngine:
    """Test suite for CloudTaskEngine."""

    def test_check_and_resume_tasks_multiple_providers(self, tmp_path):
        """Test resuming paused tasks across different cloud task providers."""
        client1 = MockCloudTaskClient(name="provider-1")
        client1.tasks = [
            CloudTask(task_id="p1_task1", state=CloudTaskState.PAUSED, title="Task 1"),
            CloudTask(task_id="p1_task2", state=CloudTaskState.RUNNING, title="Task 2"),
        ]

        client2 = MockCloudTaskClient(name="provider-2")
        client2.tasks = [
            CloudTask(task_id="p2_task1", state=CloudTaskState.PAUSED, title="Task 3"),
            CloudTask(task_id="p2_task2", state=CloudTaskState.COMPLETED, title="Task 4"),
        ]

        state_file = str(tmp_path / "cloud_task_state.json")
        engine = CloudTaskEngine(clients=[client1, client2], state_file=state_file)

        actions = engine.check_and_resume_tasks()

        assert len(actions) == 2
        assert "p1_task1" in client1.resumed_tasks
        assert "p2_task1" in client2.resumed_tasks
        assert "p1_task2" not in client1.resumed_tasks
        assert "p2_task2" not in client2.resumed_tasks

    def test_target_closed_skips_resume(self, tmp_path):
        """Test that tasks whose target issue/PR is already closed are not resumed."""
        client = MockCloudTaskClient()
        client.tasks = [
            CloudTask(task_id="task_closed_target", state=CloudTaskState.PAUSED),
        ]

        state_file = str(tmp_path / "cloud_task_state.json")
        engine = CloudTaskEngine(clients=[client], state_file=state_file)

        mock_github = MagicMock()
        mock_github.get_issue.return_value = {"state": "closed"}

        with patch("auto_coder.cloud_task_engine.GitHubClient.get_instance", return_value=mock_github):
            with patch("auto_coder.cloud_task_engine.CloudManager.get_issue_by_session", return_value=123):
                actions = engine.check_and_resume_tasks(repo_name="owner/repo")
                assert len(actions) == 0
                assert "task_closed_target" not in client.resumed_tasks
