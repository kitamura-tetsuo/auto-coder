"""Tests for issue_processor branch creation behavior."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly


def _cmd_result(success=True, stdout="", stderr="", returncode=0):
    class R:
        def __init__(self):
            self.success = success
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return R()


def test_parent_issue_branch_creation_uses_main_base():
    """When creating a missing parent issue branch, base_branch must be MAIN_BRANCH."""
    repo_name = "owner/repo"
    issue_number = 999
    parent_issue_number = 123
    issue_data = {"number": issue_number, "title": "Test"}
    config = AutomationConfig()

    # Capture calls to branch_context
    captured_calls = []

    @contextmanager
    def fake_branch_context(*args, **kwargs):
        captured_calls.append((args, kwargs))
        yield

    @contextmanager
    def fake_label_manager(*_args, **_kwargs):
        yield True

    # CommandExecutor instance in issue_processor module
    with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
        # Simulate: parent branch missing, work branch missing
        mock_cmd.run_command.side_effect = [
            _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse parent
            _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse work
        ]

        with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                # Minimal GitHub client mock
                github_client = MagicMock()
                github_client.get_parent_issue.return_value = parent_issue_number

                # Avoid deeper execution
                class DummyLLM:
                    def _run_llm_cli(self, *_args, **_kwargs):
                        return None

                with patch("src.auto_coder.issue_processor.get_llm_backend_manager", return_value=DummyLLM()):
                    _apply_issue_actions_directly(
                        repo_name,
                        issue_data,
                        config,
                        github_client,
                    )

    # First branch_context call should be for creating the parent branch from MAIN_BRANCH
    assert captured_calls, "branch_context was not called"
    first_args, first_kwargs = captured_calls[0]
    # args[0] should be the branch name
    assert first_args[0] == f"issue-{parent_issue_number}"
    assert first_kwargs.get("create_new") is True
    assert first_kwargs.get("base_branch") == config.MAIN_BRANCH
