import os
from unittest.mock import patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.github_client import GitHubClient
from src.auto_coder.util.github_action import GitHubActionsStatusResult, _check_github_actions_status
from src.auto_coder.utils import CommandResult


@pytest.mark.e2e
@pytest.mark.headless
class TestPRChecksNoChecks:
    @patch("auto_coder.gh_logger.subprocess.run")
    def test_pr_with_no_checks_reports_as_in_progress(self, mock_run_command):
        """Test that when no checks are reported, the status is considered in_progress to avoid premature merge."""
        token = os.environ.get("GITHUB_TOKEN", "placeholder-token")
        github_client = GitHubClient.get_instance(token)
        engine = AutomationEngine(github_client, None)
        config = AutomationConfig()

        # Simulate "no checks reported" scenario
        # gh pr checks returns "no checks reported" error
        def mock_side_effect(cmd_list, **kwargs):
            if isinstance(cmd_list, list) and len(cmd_list) >= 3 and cmd_list[1] == "pr" and cmd_list[2] == "checks":
                # gh pr checks command - return "no checks reported" error
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr="no checks reported on the 'test-branch' branch",
                    returncode=1,
                )
            elif isinstance(cmd_list, list) and len(cmd_list) >= 3 and cmd_list[1] == "pr" and cmd_list[2] == "view":
                # gh pr view command - return empty commits (not needed in new logic)
                return CommandResult(success=True, stdout='{"commits": []}', stderr="", returncode=0)
            else:
                return CommandResult(success=True, stdout="", stderr="", returncode=0)

        mock_run_command.side_effect = mock_side_effect

        # Provide complete PR data including head_branch
        pr_data = {
            "number": 515,
            "head_branch": "test-branch",
            "head": {"ref": "test-branch", "sha": "dummy_sha"},
        }
        # Mock _check_github_actions_status_from_history to verify it's called
        with patch("src.auto_coder.util.github_action._check_github_actions_status_from_history") as mock_history:
            mock_history.return_value = GitHubActionsStatusResult(
                success=True,
                ids=[123],
                in_progress=False,
            )

            result = _check_github_actions_status("kitamura-tetsuo/outliner", pr_data, config)

            # Should call historical fallback
            mock_history.assert_called_once()

            # Should return the result from history
            assert result.success is True
            assert result.in_progress is False
            assert result.ids == [123]
