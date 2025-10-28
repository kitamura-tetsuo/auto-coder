"""
Tests for issue processor parent issue branch strategy.
"""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly


class TestIssueProcessorParentIssueBranch:
    """Test cases for parent issue branch strategy in issue processor."""

    @patch("src.auto_coder.git_utils.CommandExecutor")
    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.ensure_pushed")
    @patch("src.auto_coder.issue_processor.render_prompt")
    def test_create_branch_with_parent_issue_existing_parent_branch(
        self, mock_render_prompt, mock_ensure_pushed, mock_cmd, mock_git_cmd_executor
    ):
        """Test creating work branch when parent issue exists and parent branch exists."""
        # Setup
        config = AutomationConfig()
        config.MAIN_BRANCH = "main"
        
        github_client = Mock()
        github_client.get_parent_issue.return_value = 1  # Parent issue #1
        
        issue_data = {
            "number": 100,
            "title": "Sub-issue",
            "body": "Sub-issue body",
            "labels": [],
            "state": "open",
            "author": "test_user",
        }
        
        # Mock ensure_pushed
        mock_ensure_pushed.return_value = Mock(
            success=True, stdout="No unpushed commits"
        )
        
        # Mock git commands
        def cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            # Parent branch exists
            if cmd_list == ["git", "rev-parse", "--verify", "issue-1"]:
                result.success = True
                result.returncode = 0
            # Work branch does not exist yet
            elif cmd_list == ["git", "rev-parse", "--verify", "issue-100"]:
                result.success = False
                result.returncode = 128
                result.stderr = "fatal: Needed a single revision"
            # Checkout parent branch
            elif cmd_list == ["git", "checkout", "issue-1"]:
                result.success = True
                result.returncode = 0
                result.stdout = "Switched to branch 'issue-1'\n"
            # Verify current branch after checkout (for git_checkout_branch)
            elif cmd_list == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                # Return the last checked out branch
                result.success = True
                result.returncode = 0
                result.stdout = "issue-1\n"  # Will be updated based on context
            # Pull latest changes
            elif cmd_list == ["git", "pull"]:
                result.success = True
                result.returncode = 0
            # Create work branch
            elif cmd_list == ["git", "checkout", "-b", "issue-100"]:
                result.success = True
                result.returncode = 0
                result.stdout = "Switched to a new branch 'issue-100'\n"

            return result

        mock_cmd.run_command.side_effect = cmd_side_effect
        # Also mock git_utils.CommandExecutor for git_checkout_branch
        mock_git_executor_instance = Mock()
        mock_git_cmd_executor.return_value = mock_git_executor_instance

        # Track which branch we're on for verification
        current_branch = ["main"]  # Use list to allow modification in nested function

        def git_utils_cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd_list == ["git", "checkout", "issue-1"]:
                current_branch[0] = "issue-1"
                result.stdout = "Switched to branch 'issue-1'\n"
            elif cmd_list == ["git", "checkout", "-b", "issue-100"]:
                current_branch[0] = "issue-100"
                result.stdout = "Switched to a new branch 'issue-100'\n"
            elif cmd_list == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                result.stdout = f"{current_branch[0]}\n"

            return result

        mock_git_executor_instance.run_command.side_effect = git_utils_cmd_side_effect
        
        # Mock LLM client
        llm_client = Mock()
        llm_client.execute.return_value = "No changes needed"
        
        # Mock render_prompt
        mock_render_prompt.return_value = "Test prompt"
        
        # Execute
        result = _apply_issue_actions_directly(
            repo_name="owner/repo",
            issue_data=issue_data,
            config=config,
            dry_run=False,
            github_client=github_client,
            llm_client=llm_client,
            message_backend_manager=None,
        )
        
        # Verify parent issue was checked
        github_client.get_parent_issue.assert_called_once_with("owner/repo", 100)

        # Verify git commands were called in correct order
        # git_checkout_branch uses git_utils.CommandExecutor, not issue_processor.cmd
        git_calls = mock_git_executor_instance.run_command.call_args_list

        # Find the checkout calls
        checkout_calls = [
            call for call in git_calls
            if call[0][0][0] == "git" and call[0][0][1] == "checkout"
        ]

        # Should checkout parent branch (issue-1) and create work branch (issue-100)
        assert any(
            "issue-1" in call[0][0] for call in checkout_calls
        ), "Should checkout parent branch issue-1"
        assert any(
            "issue-100" in call[0][0] for call in checkout_calls
        ), "Should create work branch issue-100"

    @patch("src.auto_coder.git_utils.CommandExecutor")
    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.ensure_pushed")
    @patch("src.auto_coder.issue_processor.render_prompt")
    def test_create_branch_with_parent_issue_no_parent_branch(
        self, mock_render_prompt, mock_ensure_pushed, mock_cmd, mock_git_cmd_executor
    ):
        """Test creating work branch when parent issue exists but parent branch doesn't exist."""
        # Setup
        config = AutomationConfig()
        config.MAIN_BRANCH = "main"
        
        github_client = Mock()
        github_client.get_parent_issue.return_value = 1  # Parent issue #1
        
        issue_data = {
            "number": 100,
            "title": "Sub-issue",
            "body": "Sub-issue body",
            "labels": [],
            "state": "open",
            "author": "test_user",
        }
        
        # Mock ensure_pushed
        mock_ensure_pushed.return_value = Mock(
            success=True, stdout="No unpushed commits"
        )
        
        # Mock git commands
        def cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            # Parent branch doesn't exist
            if cmd_list == ["git", "rev-parse", "--verify", "issue-1"]:
                result.success = False
                result.returncode = 128
                result.stderr = "fatal: Needed a single revision"
            # Work branch does not exist yet
            elif cmd_list == ["git", "rev-parse", "--verify", "issue-100"]:
                result.success = False
                result.returncode = 128
                result.stderr = "fatal: Needed a single revision"
            # Checkout main branch
            elif cmd_list == ["git", "checkout", "main"]:
                result.success = True
                result.returncode = 0
            # Pull latest changes
            elif cmd_list == ["git", "pull"]:
                result.success = True
                result.returncode = 0
            # Create parent branch
            elif cmd_list == ["git", "checkout", "-b", "issue-1"]:
                result.success = True
                result.returncode = 0
            # Push parent branch
            elif cmd_list == ["git", "push", "-u", "origin", "issue-1"]:
                result.success = True
                result.returncode = 0
            # Checkout parent branch
            elif cmd_list == ["git", "checkout", "issue-1"]:
                result.success = True
            # Create work branch
            elif cmd_list == ["git", "checkout", "-b", "issue-100"]:
                result.success = True

            return result

        mock_cmd.run_command.side_effect = cmd_side_effect

        # Mock git_utils.CommandExecutor for git_checkout_branch
        mock_git_executor_instance = Mock()
        mock_git_cmd_executor.return_value = mock_git_executor_instance

        current_branch = ["main"]

        def git_utils_cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd_list == ["git", "checkout", "main"]:
                current_branch[0] = "main"
                result.stdout = "Switched to branch 'main'\n"
            elif cmd_list == ["git", "checkout", "-b", "issue-1"]:
                current_branch[0] = "issue-1"
                result.stdout = "Switched to a new branch 'issue-1'\n"
            elif cmd_list == ["git", "checkout", "issue-1"]:
                current_branch[0] = "issue-1"
                result.stdout = "Switched to branch 'issue-1'\n"
            elif cmd_list == ["git", "checkout", "-b", "issue-100"]:
                current_branch[0] = "issue-100"
                result.stdout = "Switched to a new branch 'issue-100'\n"
            elif cmd_list == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                result.stdout = f"{current_branch[0]}\n"
            elif cmd_list[0] == "git" and cmd_list[1] == "push":
                # Handle push commands
                result.stdout = "Branch pushed successfully\n"

            return result

        mock_git_executor_instance.run_command.side_effect = git_utils_cmd_side_effect
        
        # Mock LLM client
        llm_client = Mock()
        llm_client.execute.return_value = "No changes needed"
        
        # Mock render_prompt
        mock_render_prompt.return_value = "Test prompt"
        
        # Execute
        result = _apply_issue_actions_directly(
            repo_name="owner/repo",
            issue_data=issue_data,
            config=config,
            dry_run=False,
            github_client=github_client,
            llm_client=llm_client,
            message_backend_manager=None,
        )
        
        # Verify parent issue was checked
        github_client.get_parent_issue.assert_called_once_with("owner/repo", 100)

        # Verify git commands were called
        # git_checkout_branch uses git_utils.CommandExecutor, not issue_processor.cmd
        git_calls = mock_git_executor_instance.run_command.call_args_list

        # Should checkout main, create parent branch, and create work branch
        checkout_calls = [
            call for call in git_calls
            if call[0][0][0] == "git" and call[0][0][1] == "checkout"
        ]

        assert any(
            call[0][0] == ["git", "checkout", "main"] for call in checkout_calls
        ), "Should checkout main branch"
        assert any(
            call[0][0] == ["git", "checkout", "-b", "issue-1"] for call in checkout_calls
        ), "Should create parent branch issue-1"
        assert any(
            call[0][0] == ["git", "checkout", "-b", "issue-100"] for call in checkout_calls
        ), "Should create work branch issue-100"

        # Verify push was called for parent branch (this uses git_utils.CommandExecutor)
        push_calls = [
            call for call in git_calls
            if call[0][0][0] == "git" and call[0][0][1] == "push"
        ]
        assert any(
            call[0][0] == ["git", "push", "-u", "origin", "issue-1"] for call in push_calls
        ), "Should push parent branch issue-1"
        assert any(
            call[0][0] == ["git", "push", "-u", "origin", "issue-100"] for call in push_calls
        ), "Should push work branch issue-100"

    @patch("src.auto_coder.git_utils.CommandExecutor")
    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.ensure_pushed")
    @patch("src.auto_coder.issue_processor.render_prompt")
    def test_create_branch_without_parent_issue(
        self, mock_render_prompt, mock_ensure_pushed, mock_cmd, mock_git_cmd_executor
    ):
        """Test creating work branch when no parent issue exists."""
        # Setup
        config = AutomationConfig()
        config.MAIN_BRANCH = "main"
        
        github_client = Mock()
        github_client.get_parent_issue.return_value = None  # No parent issue
        
        issue_data = {
            "number": 100,
            "title": "Top-level issue",
            "body": "Top-level issue body",
            "labels": [],
            "state": "open",
            "author": "test_user",
        }
        
        # Mock ensure_pushed
        mock_ensure_pushed.return_value = Mock(
            success=True, stdout="No unpushed commits"
        )
        
        # Mock git commands
        def cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            # Work branch does not exist yet
            if cmd_list == ["git", "rev-parse", "--verify", "issue-100"]:
                result.success = False
                result.returncode = 128
                result.stderr = "fatal: Needed a single revision"
            # Checkout main branch
            elif cmd_list == ["git", "checkout", "main"]:
                result.success = True
                result.returncode = 0
            # Pull latest changes
            elif cmd_list == ["git", "pull"]:
                result.success = True
                result.returncode = 0
            # Create work branch
            elif cmd_list == ["git", "checkout", "-b", "issue-100"]:
                result.success = True
                result.returncode = 0

            return result

        mock_cmd.run_command.side_effect = cmd_side_effect

        # Mock git_utils.CommandExecutor for git_checkout_branch
        mock_git_executor_instance = Mock()
        mock_git_cmd_executor.return_value = mock_git_executor_instance

        current_branch = ["main"]

        def git_utils_cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd_list == ["git", "checkout", "main"]:
                current_branch[0] = "main"
                result.stdout = "Switched to branch 'main'\n"
            elif cmd_list == ["git", "checkout", "-b", "issue-100"]:
                current_branch[0] = "issue-100"
                result.stdout = "Switched to a new branch 'issue-100'\n"
            elif cmd_list == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                result.stdout = f"{current_branch[0]}\n"

            return result

        mock_git_executor_instance.run_command.side_effect = git_utils_cmd_side_effect
        
        # Mock LLM client
        llm_client = Mock()
        llm_client.execute.return_value = "No changes needed"
        
        # Mock render_prompt
        mock_render_prompt.return_value = "Test prompt"
        
        # Execute
        result = _apply_issue_actions_directly(
            repo_name="owner/repo",
            issue_data=issue_data,
            config=config,
            dry_run=False,
            github_client=github_client,
            llm_client=llm_client,
            message_backend_manager=None,
        )
        
        # Verify parent issue was checked
        github_client.get_parent_issue.assert_called_once_with("owner/repo", 100)
        
        # Verify git commands were called
        # git_checkout_branch uses git_utils.CommandExecutor, not issue_processor.cmd
        git_calls = mock_git_executor_instance.run_command.call_args_list

        # Should checkout main branch and create work branch from main
        checkout_calls = [
            call for call in git_calls
            if call[0][0][0] == "git" and call[0][0][1] == "checkout"
        ]

        assert any(
            call[0][0] == ["git", "checkout", "main"] for call in checkout_calls
        ), "Should checkout main branch"
        assert any(
            call[0][0] == ["git", "checkout", "-b", "issue-100"] for call in checkout_calls
        ), "Should create work branch issue-100"

    @patch("src.auto_coder.git_utils.CommandExecutor")
    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.ensure_pushed")
    @patch("src.auto_coder.issue_processor.render_prompt")
    def test_use_existing_work_branch_with_parent_issue(
        self, mock_render_prompt, mock_ensure_pushed, mock_cmd, mock_git_cmd_executor
    ):
        """Test using existing work branch when it already exists."""
        # Setup
        config = AutomationConfig()
        config.MAIN_BRANCH = "main"

        github_client = Mock()
        github_client.get_parent_issue.return_value = 1  # Parent issue #1

        issue_data = {
            "number": 100,
            "title": "Sub-issue",
            "body": "Sub-issue body",
            "labels": [],
            "state": "open",
            "author": "test_user",
        }

        # Mock ensure_pushed
        mock_ensure_pushed.return_value = Mock(
            success=True, stdout="No unpushed commits"
        )

        # Mock git commands
        def cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            # Parent branch exists
            if cmd_list == ["git", "rev-parse", "--verify", "issue-1"]:
                result.success = True
                result.returncode = 0
            # Work branch already exists
            elif cmd_list == ["git", "rev-parse", "--verify", "issue-100"]:
                result.success = True
                result.returncode = 0
            # Checkout existing work branch
            elif cmd_list == ["git", "checkout", "issue-100"]:
                result.success = True
                result.returncode = 0

            return result

        mock_cmd.run_command.side_effect = cmd_side_effect

        # Mock git_utils.CommandExecutor for git_checkout_branch
        mock_git_executor_instance = Mock()
        mock_git_cmd_executor.return_value = mock_git_executor_instance

        current_branch = ["main"]

        def git_utils_cmd_side_effect(cmd_list, **kwargs):
            result = Mock()
            result.success = True
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd_list == ["git", "checkout", "issue-100"]:
                current_branch[0] = "issue-100"
                result.stdout = "Switched to branch 'issue-100'\n"
            elif cmd_list == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                result.stdout = f"{current_branch[0]}\n"

            return result

        mock_git_executor_instance.run_command.side_effect = git_utils_cmd_side_effect

        # Mock LLM client
        llm_client = Mock()
        llm_client.execute.return_value = "No changes needed"

        # Mock render_prompt
        mock_render_prompt.return_value = "Test prompt"

        # Execute
        result = _apply_issue_actions_directly(
            repo_name="owner/repo",
            issue_data=issue_data,
            config=config,
            dry_run=False,
            github_client=github_client,
            llm_client=llm_client,
            message_backend_manager=None,
        )

        # Verify parent issue was checked
        github_client.get_parent_issue.assert_called_once_with("owner/repo", 100)

        # Verify git commands were called
        # git_checkout_branch uses git_utils.CommandExecutor, not issue_processor.cmd
        git_calls = mock_git_executor_instance.run_command.call_args_list

        # Find the checkout calls
        checkout_calls = [
            call for call in git_calls
            if call[0][0][0] == "git" and call[0][0][1] == "checkout"
        ]

        # Should only checkout existing work branch, not create new one or checkout parent
        assert len(checkout_calls) == 1, "Should only checkout existing work branch"
        assert checkout_calls[0][0][0] == ["git", "checkout", "issue-100"], "Should checkout issue-100"

        # Should NOT checkout parent branch or main branch
        assert not any(
            "issue-1" in call[0][0] for call in checkout_calls
        ), "Should NOT checkout parent branch when work branch exists"
        assert not any(
            "main" in call[0][0] for call in checkout_calls
        ), "Should NOT checkout main branch when work branch exists"

        # Should NOT create new branch (no -b flag)
        assert not any(
            "-b" in call[0][0] for call in checkout_calls
        ), "Should NOT create new branch when work branch exists"

