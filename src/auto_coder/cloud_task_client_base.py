"""
Base abstraction for asynchronous cloud coding tasks.

This module defines common interfaces and state models for cloud tasks
executed by providers such as Jules, Claude Routine, and Codex Cloud.
"""

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, List, Optional

from .llm_client_base import LLMClientBase


class CloudTaskState(Enum):
    """Normalized state of a cloud task."""

    QUEUED = auto()
    RUNNING = auto()
    PAUSED = auto()
    COMPLETED = auto()
    FAILED = auto()
    UNKNOWN = auto()


@dataclass
class CloudTask:
    """Represents a cloud task across different cloud execution providers."""

    task_id: str = ""
    state: CloudTaskState = CloudTaskState.UNKNOWN
    raw_state: Optional[str] = None
    title: Optional[str] = None
    pull_request: Optional[Any] = None
    prompt: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    url: Optional[str] = None
    error: Optional[str] = None
    raw_data: Optional[Any] = None


class CloudTaskClientBase(LLMClientBase):
    """Base class for asynchronous cloud coding task clients.

    Extends LLMClientBase to manage the lifecycle of cloud tasks, including
    starting tasks, checking state, and resuming paused tasks.
    """

    @abstractmethod
    def continue_if_paused(self, task_id: str) -> bool:
        """Resume a cloud task if it is currently paused or stopped in a continuable state.

        Args:
            task_id: The ID of the cloud task / session.

        Returns:
            True if the task was in a paused state and resumed, False otherwise.
        """
        pass

    @abstractmethod
    def start_task(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
    ) -> str:
        """Start a new cloud task.

        Args:
            prompt: The instruction / task prompt.
            repo_name: Target repository name (e.g. 'owner/repo').
            base_branch: Target base branch name (e.g. 'main').
            title: Optional title/description for the task.

        Returns:
            Task ID / Session ID string.
        """
        pass

    def get_task(self, task_id: str) -> Optional[CloudTask]:
        """Retrieve the normalized details for a specific cloud task.

        Args:
            task_id: The task ID.

        Returns:
            CloudTask instance or None if not found.
        """
        return None

    def list_tasks(self, repo_name: Optional[str] = None) -> List[CloudTask]:
        """List active or recent cloud tasks.

        Args:
            repo_name: Optional repository name to filter tasks by.

        Returns:
            List of CloudTask instances.
        """
        return []

    def stop_task(self, task_id: str) -> bool:
        """Stop or cancel a cloud task.

        Args:
            task_id: The task ID.

        Returns:
            True if successfully stopped, False otherwise.
        """
        return False
