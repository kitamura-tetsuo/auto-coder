"""
Unit tests for CloudTaskClientBase, CloudTaskState, and CloudTask.
"""

from datetime import datetime, timezone
from typing import List, Optional

import pytest

from auto_coder.cloud_task_client_base import (
    CloudTask,
    CloudTaskClientBase,
    CloudTaskState,
)


class DummyCloudTaskClient(CloudTaskClientBase):
    """Concrete implementation of CloudTaskClientBase for testing."""

    def __init__(self, paused_task_ids: Optional[List[str]] = None) -> None:
        super().__init__()
        self.paused_task_ids = paused_task_ids or []
        self.resumed_task_ids: List[str] = []
        self.tasks: List[CloudTask] = []

    def continue_if_paused(self, task_id: str) -> bool:
        if task_id in self.paused_task_ids:
            self.resumed_task_ids.append(task_id)
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
        task = CloudTask(
            task_id=tid,
            state=CloudTaskState.RUNNING,
            title=title,
            prompt=prompt,
            created_at=datetime.now(timezone.utc),
        )
        self.tasks.append(task)
        return tid

    def get_task(self, task_id: str) -> Optional[CloudTask]:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def list_tasks(self, repo_name: Optional[str] = None) -> List[CloudTask]:
        return list(self.tasks)

    def stop_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if task:
            task.state = CloudTaskState.FAILED
            return True
        return False

    def _run_llm_cli(self, prompt: str) -> str:
        tid = self.start_task(prompt)
        return f"Started: {tid}"

    def check_mcp_server_configured(self, server_name: str) -> bool:
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        return False


class TestCloudTaskClientBase:
    """Test suite for CloudTaskClientBase abstraction."""

    def test_cloud_task_dataclass_defaults(self):
        """Test default values of CloudTask dataclass."""
        task = CloudTask()
        assert task.task_id == ""
        assert task.state == CloudTaskState.UNKNOWN
        assert task.raw_state is None
        assert task.title is None
        assert task.pull_request is None
        assert task.prompt is None
        assert task.created_at is None
        assert task.updated_at is None
        assert task.url is None
        assert task.error is None
        assert task.raw_data is None

    def test_cloud_task_state_enum(self):
        """Test CloudTaskState enum values."""
        assert CloudTaskState.QUEUED is not None
        assert CloudTaskState.RUNNING is not None
        assert CloudTaskState.PAUSED is not None
        assert CloudTaskState.COMPLETED is not None
        assert CloudTaskState.FAILED is not None
        assert CloudTaskState.UNKNOWN is not None

    def test_client_start_and_get_task(self):
        """Test start_task and get_task on concrete client."""
        client = DummyCloudTaskClient()
        tid = client.start_task("Fix bug", repo_name="owner/repo", base_branch="main", title="Bugfix")

        assert tid == "task_1"
        task = client.get_task("task_1")
        assert task is not None
        assert task.task_id == "task_1"
        assert task.title == "Bugfix"
        assert task.prompt == "Fix bug"
        assert task.state == CloudTaskState.RUNNING

    def test_client_continue_if_paused(self):
        """Test continue_if_paused behaviour on concrete client."""
        client = DummyCloudTaskClient(paused_task_ids=["task_paused_1"])

        assert client.continue_if_paused("task_paused_1") is True
        assert "task_paused_1" in client.resumed_task_ids

        assert client.continue_if_paused("task_running_2") is False

    def test_client_stop_task(self):
        """Test stop_task on concrete client."""
        client = DummyCloudTaskClient()
        tid = client.start_task("Work on feature")

        task = client.get_task(tid)
        assert task.state == CloudTaskState.RUNNING

        stopped = client.stop_task(tid)
        assert stopped is True
        assert task.state == CloudTaskState.FAILED
