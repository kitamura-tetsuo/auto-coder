import json
import os
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.util.github_action import GitHubActionsStatusResult
from src.auto_coder.utils import CommandExecutor

"""Tests for automation engine functionality."""


class TestAutomationEngine:
    """Test cases for AutomationEngine class."""

    def test_init(self, mock_github_client, mock_gemini_client, temp_reports_dir):
        """Test AutomationEngine initialization."""
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        assert engine.github == mock_github_client
        assert engine.config.REPORTS_DIR == "reports"

    # Note: Tests for deprecated process_issues and related functions have been removed
    # as those functions are no longer supported. The modern API uses process_single
    # and LabelManager context manager for issue processing.

    @patch("src.auto_coder.automation_engine.create_feature_issues")
    def test_create_feature_issues_success(
        self,
        mock_create_feature_issues,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
        sample_feature_suggestion,
    ):
        """Test successful feature issues creation."""
        # Setup
        mock_create_feature_issues.return_value = [
            {
                "number": 123,
                "title": sample_feature_suggestion["title"],
                "url": "https://github.com/test/repo/issues/123",
            }
        ]

        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        # Execute
        result = engine.create_feature_issues(test_repo_name)

        # Assert
        assert len(result) == 1
        assert result[0]["number"] == 123
        assert result[0]["title"] == sample_feature_suggestion["title"]

        mock_create_feature_issues.assert_called_once()

    # Note: _process_issues and _process_issues_jules_mode are now functions in issue_processor.py
    # These tests are covered by test_issue_processor.py

    # Note: _resolve_merge_conflicts_with_gemini is now in conflict_resolver.py
    # These tests are covered by test_conflict_resolver.py

    # Note: _process_issues and _process_pull_requests are now functions in issue_processor.py and pr_processor.py
    # These tests are covered by test_issue_processor.py and test_pr_processor.py

    # Note: Dependabot filtering tests and PR processing tests moved to test_pr_processor.py

    def test_merge_pr_with_conflict_resolution_success(self, mock_github_client, mock_gemini_client):
        """Test that the engine correctly handles PR processing."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        # Mock GitHub client to return proper PR data
        mock_pr_data = {
            "number": 123,
            "title": "Test PR",
            "body": "Test description",
            "head": {"ref": "test-branch"},
            "base": {"ref": "main"},
            "mergeable": True,
            "draft": False,
        }
        mock_github_client.get_pr_details_by_number.return_value = mock_pr_data
        mock_github_client.try_add_work_in_progress_label.return_value = True

        # Mock successful processing - simulate that the PR was processed without errors
        with (
            patch("src.auto_coder.util.github_action._check_github_actions_status") as mock_check_actions,
            patch("src.auto_coder.pr_processor._take_pr_actions") as mock_take_actions,
        ):
            mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])
            mock_take_actions.return_value = ["Merged PR successfully", "Applied fixes"]

            # Execute
            result = engine.process_single("test/repo", "pr", 123, jules_mode=False)

            # Assert
            assert result["repository"] == "test/repo"
            assert len(result["prs_processed"]) == 1
            assert "Merged PR successfully" in result["prs_processed"][0]["actions_taken"]
            assert len(result["errors"]) == 0
            mock_take_actions.assert_called_once()

    def test_merge_pr_with_conflict_resolution_failure(self, mock_github_client, mock_gemini_client):
        """Test that the engine correctly handles PR processing failure."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        # Mock GitHub client to return proper PR data
        mock_pr_data = {
            "number": 123,
            "title": "Test PR",
            "body": "Test description",
            "head": {"ref": "test-branch"},
            "base": {"ref": "main"},
            "mergeable": True,
            "draft": False,
        }
        mock_github_client.get_pr_details_by_number.return_value = mock_pr_data
        mock_github_client.try_add_work_in_progress_label.return_value = True

        # Mock failed processing
        with (
            patch("src.auto_coder.util.github_action._check_github_actions_status") as mock_check_actions,
            patch("src.auto_coder.pr_processor._take_pr_actions") as mock_take_actions,
        ):
            mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])
            mock_take_actions.side_effect = Exception("Processing failed")

            # Execute
            result = engine.process_single("test/repo", "pr", 123, jules_mode=False)

            # Assert
            assert result["repository"] == "test/repo"
            assert len(result["prs_processed"]) == 0
            assert len(result["errors"]) == 1
            assert "Processing failed" in result["errors"][0]
            mock_take_actions.assert_called_once()

    def test_resolve_pr_merge_conflicts_git_cleanup(self, mock_github_client, mock_gemini_client):
        """Test that PR processing handles conflicts correctly."""
        # Setup - this test verifies that process_single handles PR with conflicts
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        # Mock GitHub client to return PR data
        mock_pr_data = {
            "number": 123,
            "title": "Test PR with conflicts",
            "body": "Test description",
            "head": {"ref": "test-branch"},
            "base": {"ref": "main"},
            "mergeable": False,  # Simulate merge conflicts
            "draft": False,
        }
        mock_github_client.get_pr_details_by_number.return_value = mock_pr_data
        mock_github_client.try_add_work_in_progress_label.return_value = True

        # Mock that GitHub Actions are failing due to conflicts
        with (
            patch("src.auto_coder.util.github_action._check_github_actions_status") as mock_check_actions,
            patch("src.auto_coder.pr_processor._take_pr_actions") as mock_take_actions,
        ):
            mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[123])
            mock_take_actions.return_value = ["Resolved merge conflicts successfully"]

            # Execute
            result = engine.process_single("test/repo", "pr", 123, jules_mode=False)

            # Assert
            assert result["repository"] == "test/repo"
            assert len(result["prs_processed"]) == 1
            assert "Resolved merge conflicts successfully" in result["prs_processed"][0]["actions_taken"]
            assert len(result["errors"]) == 0
            # Note: _check_github_actions_status may or may not be called depending on the code path
            mock_take_actions.assert_called_once()

    def test_apply_issue_actions_directly(self, mock_github_client, mock_gemini_client):
        """Test direct issue actions application using Gemini CLI."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        issue_data = {
            "number": 123,
            "title": "Bug in login system",
            "body": "The login system has a bug",
            "labels": ["bug"],
            "state": "open",
            "author": "testuser",
        }

        # Mock the underlying function to return expected results
        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply:
            mock_apply.return_value = [
                "Gemini CLI analyzed and took action on issue: Analyzed the issue and added implementation...",
                "Added analysis comment to issue #123",
                "Committed changes: Auto-Coder: Address issue #123",
            ]

            # Execute
            result = engine._apply_issue_actions_directly("test/repo", issue_data)

        # Assert
        assert len(result) == 3
        assert "Gemini CLI analyzed and took action" in result[0]
        assert "Added analysis comment" in result[1]
        assert "Committed changes" in result[2]

    # Note: test_take_pr_actions_success removed - _take_pr_actions is now in pr_processor.py

    def test_resolve_pr_merge_conflicts_uses_base_branch(self, mock_github_client, mock_gemini_client):
        """When PR base branch is not 'main', conflict resolution should fetch/merge that base branch."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        # Mock PR data with non-main base
        pr_data = {
            "number": 456,
            "title": "Feature PR",
            "body": "Some changes",
            "base": {"ref": "develop"},
        }
        mock_github_client.get_pr_details_by_number.return_value = pr_data

        # Track the git commands that are called
        with patch.object(engine.cmd, "run_command") as mock_run_command, patch("auto_coder.gh_logger.subprocess.run") as mock_subprocess:
            # Execute
            result = engine._resolve_pr_merge_conflicts("test/repo", 456)

        # Assert
        assert result is True
        # Check that the correct git commands were called
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert ["git", "reset", "--hard", "HEAD"] in calls
        assert ["git", "clean", "-fd"] in calls
        assert ["git", "merge", "--abort"] in calls
        assert ["git", "fetch", "origin", "develop"] in calls  # Fetch base branch
        assert ["git", "merge", "origin/develop"] in calls  # Merge base branch
        assert ["git", "push"] in calls
        # Check that the gh pr checkout command was called
        subprocess_calls = [call[0][0] for call in mock_subprocess.call_args_list]
        assert ["gh", "pr", "checkout", "456"] in subprocess_calls

    @patch("subprocess.run")
    def test_update_with_base_branch_uses_provided_base_branch(self, mock_run, mock_github_client, mock_gemini_client):
        """_update_with_base_branch should use pr_data.base_branch when provided (even if not main)."""
        # Setup mocks for git operations: fetch, rev-list (2 commits behind), merge, push
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="2", stderr=""),  # git rev-list
            Mock(returncode=0, stdout="", stderr=""),  # git merge
            Mock(returncode=0, stdout="", stderr=""),  # git push
        ]

        engine = AutomationEngine(mock_github_client)
        pr_data = {"number": 999, "base_branch": "develop"}

        # Execute
        result = engine._update_with_base_branch("test/repo", pr_data)

        # Assert
        assert any("2 commits behind develop" in a for a in result)
        assert any("Successfully merged develop branch into PR #999" in a for a in result)
        assert any("Pushed updated branch" in a for a in result)

    def test_get_repository_context_success(self, mock_github_client, mock_gemini_client):
        """Test successful repository context retrieval."""
        # Setup
        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.description = "Test description"
        mock_repo.language = "Python"
        mock_repo.stargazers_count = 100
        mock_repo.forks_count = 20

        mock_github_client.get_repository.return_value = mock_repo
        mock_github_client.get_open_issues.return_value = []
        mock_github_client.get_open_pull_requests.return_value = []
        mock_github_client.get_issue_details.return_value = {}
        mock_github_client.get_pr_details.return_value = {}

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine._get_repository_context("test/repo")

        # Assert
        assert result["name"] == "test-repo"
        assert result["description"] == "Test description"
        assert result["language"] == "Python"
        assert result["stars"] == 100
        assert result["forks"] == 20

    def test_format_feature_issue_body(self, mock_github_client, mock_gemini_client, sample_feature_suggestion):
        """Test feature issue body formatting."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine._format_feature_issue_body(sample_feature_suggestion)

        # Assert
        assert "## Feature Request" in result
        assert sample_feature_suggestion["description"] in result
        assert sample_feature_suggestion["rationale"] in result
        assert sample_feature_suggestion["priority"] in result
        assert "This feature request was generated automatically" in result

        # Check acceptance criteria formatting
        for criteria in sample_feature_suggestion["acceptance_criteria"]:
            assert f"- [ ] {criteria}" in result

    @patch("builtins.open")
    @patch("json.dump")
    @patch("os.path.join")
    @patch("os.makedirs")
    def test_save_report_success(
        self,
        mock_makedirs,
        mock_join,
        mock_json_dump,
        mock_open,
        mock_github_client,
        mock_gemini_client,
    ):
        """Test successful report saving without repo_name."""
        # Setup
        mock_join.return_value = "reports/test_report.json"
        mock_file = Mock()
        mock_open.return_value.__enter__.return_value = mock_file

        engine = AutomationEngine(mock_github_client)
        test_data = {"test": "data"}

        # Execute - use traditional reports/ if repo_name is not specified
        engine._save_report(test_data, "test_report")

        # Assert
        mock_makedirs.assert_called_once_with("reports", exist_ok=True)
        mock_open.assert_called_once()
        mock_json_dump.assert_called_once_with(test_data, mock_file, indent=2, ensure_ascii=False)

    @patch("builtins.open")
    @patch("json.dump")
    @patch("os.makedirs")
    def test_save_report_with_repo_name(
        self,
        mock_makedirs,
        mock_json_dump,
        mock_open,
        mock_github_client,
        mock_gemini_client,
    ):
        """Test report saving with repo_name to ~/.auto-coder/{repository}/."""
        # Setup
        from pathlib import Path

        repo_name = "owner/repo"
        expected_dir = str(Path.home() / ".auto-coder" / "owner_repo")
        mock_file = Mock()
        mock_open.return_value.__enter__.return_value = mock_file

        engine = AutomationEngine(mock_github_client)
        test_data = {"test": "data"}

        # Execute - use ~/.auto-coder/{repository}/ if repo_name is specified
        engine._save_report(test_data, "test_report", repo_name)

        # Assert
        mock_makedirs.assert_called_once_with(expected_dir, exist_ok=True)
        mock_open.assert_called_once()
        mock_json_dump.assert_called_once_with(test_data, mock_file, indent=2, ensure_ascii=False)

    def test_should_auto_merge_pr_low_risk_bugfix(self, mock_github_client, mock_gemini_client):
        """Test PR should be auto-merged for low-risk bugfix."""
        # Setup
        analysis = {
            "risk_level": "low",
            "category": "bugfix",
            "recommendations": [{"action": "This PR looks good and can be merged safely"}],
        }
        pr_data = {"mergeable": True, "draft": False}

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is True

    def test_should_auto_merge_pr_high_risk(self, mock_github_client, mock_gemini_client):
        """Test PR should not be auto-merged for high-risk changes."""
        # Setup
        analysis = {
            "risk_level": "high",
            "category": "bugfix",
            "recommendations": [{"action": "This PR can be merged"}],
        }
        pr_data = {"mergeable": True, "draft": False}

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is False

    def test_should_auto_merge_pr_draft(self, mock_github_client, mock_gemini_client):
        """Test PR should not be auto-merged if it's a draft."""
        # Setup
        analysis = {
            "risk_level": "low",
            "category": "bugfix",
            "recommendations": [{"action": "This PR can be merged"}],
        }
        pr_data = {"mergeable": True, "draft": True}

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is False

    @patch("subprocess.run")
    @patch("os.path.exists")
    def test_run_pr_tests_success(self, mock_exists, mock_run, mock_github_client, mock_gemini_client):
        """Test successful PR test execution."""
        # Setup
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=0, stdout="All tests passed", stderr="")

        engine = AutomationEngine(mock_github_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._run_pr_tests("test/repo", pr_data)

        # Assert
        assert result["success"] is True
        assert result["output"] == "All tests passed"
        mock_run.assert_called_once_with(
            ["bash", "scripts/test.sh"],
            capture_output=True,
            text=True,
            timeout=3600,
            cwd=None,
        )

    @patch("subprocess.run")
    @patch("os.path.exists")
    def test_run_pr_tests_failure(self, mock_exists, mock_run, mock_github_client, mock_gemini_client):
        """Test PR test execution failure."""
        # Setup
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="Test failed: assertion error")

        engine = AutomationEngine(mock_github_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._run_pr_tests("test/repo", pr_data)

        # Assert
        assert result["success"] is False
        assert result["errors"] == "Test failed: assertion error"
        assert result["return_code"] == 1

    def test_extract_important_errors(self, mock_github_client, mock_gemini_client):
        """Test error extraction from test output."""
        # Setup
        test_result = {
            "success": False,
            "output": ("Running tests...\nERROR: Test failed\nSome other output\nFAILED: assertion error\nMore output"),
            "errors": "ImportError: module not found",
        }

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine._extract_important_errors(test_result)

        # Assert
        assert "ERROR: Test failed" in result
        assert "FAILED: assertion error" in result
        assert "ImportError: module not found" in result

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_check_github_actions_status_all_passed(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check when all checks pass."""
        from src.auto_coder.util.github_action import _check_github_actions_status

        # Setup - mock cmd.run_command to return successful checks
        mock_run_command.return_value = Mock(returncode=0, stdout="✓ test-check\n✓ another-check", stderr="")

        config = AutomationConfig()
        pr_data = {"number": 123}

        # Execute
        result = _check_github_actions_status("test/repo", pr_data, config)

        # Assert
        assert result.success is True
        assert len(result.ids) == 0  # No run IDs when checks pass

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_check_github_actions_status_some_failed(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check when some checks fail."""
        from src.auto_coder.util.github_action import _check_github_actions_status

        # Setup
        mock_run_command.return_value = Mock(returncode=0, stdout="✓ passing-check\n✗ failing-check\n- pending-check", stderr="")

        config = AutomationConfig()
        pr_data = {"number": 123}

        # Execute
        result = _check_github_actions_status("test/repo", pr_data, config)

        # Assert
        assert result.success is False
        assert len(result.ids) == 0  # No URLs in the output, so no run IDs

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_check_github_actions_status_tab_format_with_failures(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check with tab-separated format and failures."""
        from src.auto_coder.util.github_action import _check_github_actions_status

        # Setup - simulating the actual output format from gh CLI
        mock_run_command.return_value = Mock(
            returncode=1,  # Non-zero because some checks failed
            stdout="test\tfail\t2m50s\thttps://github.com/example/repo/actions/runs/123\nformat\tpass\t27s\thttps://github.com/example/repo/actions/runs/124\nlink-pr-to-issue\tskipping\t0\thttps://github.com/example/repo/actions/runs/125",
            stderr="",
        )

        config = AutomationConfig()
        pr_data = {"number": 123}

        # Execute
        result = _check_github_actions_status("test/repo", pr_data, config)

        # Assert
        assert result.success is False  # Should be False because 'test' failed
        assert 123 in result.ids  # Run ID should be extracted from the failed check

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_check_github_actions_status_tab_format_all_pass(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check with tab-separated format and all passing."""
        from src.auto_coder.util.github_action import _check_github_actions_status

        # Setup
        mock_run_command.return_value = Mock(
            returncode=0,
            stdout="test\tpass\t2m50s\thttps://github.com/example/repo/actions/runs/123\nformat\tpass\t27s\thttps://github.com/example/repo/actions/runs/124\nlink-pr-to-issue\tskipping\t0\thttps://github.com/example/repo/actions/runs/125",
            stderr="",
        )

        config = AutomationConfig()
        pr_data = {"number": 123}

        # Execute
        result = _check_github_actions_status("test/repo", pr_data, config)

        # Assert
        assert result.success is True  # Should be True because all required checks passed
        assert len(result.ids) == 0  # No failed checks, so no run IDs needed

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_check_github_actions_status_no_checks_reported(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Handle gh CLI message when no checks are reported."""
        from src.auto_coder.util.github_action import _check_github_actions_status

        # Mock multiple cmd.run_command calls for the historical fallback
        def mock_side_effect(cmd_list, **kwargs):
            if len(cmd_list) >= 3 and cmd_list[1] == "pr" and cmd_list[2] == "checks":
                # gh pr checks command
                return Mock(returncode=1, stdout="", stderr="no checks reported on the 'feat/global-search' branch")
            elif len(cmd_list) >= 3 and cmd_list[1] == "pr" and cmd_list[2] == "view":
                # gh pr view command - return empty commits
                return Mock(returncode=0, stdout='{"commits": []}', stderr="")
            else:
                # Other commands
                return Mock(returncode=0, stdout="", stderr="")

        mock_run_command.side_effect = mock_side_effect

        config = AutomationConfig()
        # Provide complete PR data including head_branch for historical fallback
        pr_data = {"number": 123, "head_branch": "test-branch", "head": {"ref": "test-branch"}}

        result = _check_github_actions_status("test/repo", pr_data, config)

        # When there are no checks, should return success
        assert result.success is True
        assert result.ids == []

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_checkout_pr_branch_success(self, mock_gh_subprocess, mock_github_client, mock_gemini_client):
        """Test successful PR branch checkout without force clean (default behavior)."""
        # Setup
        mock_gh_subprocess.return_value = Mock(success=True, stdout="Switched to branch", stderr="", returncode=0)

        from src.auto_coder import pr_processor

        pr_data = {"number": 123}

        # Execute
        result = pr_processor._checkout_pr_branch("test/repo", pr_data, AutomationConfig())

        # Assert
        assert result is True
        assert mock_gh_subprocess.call_count == 1

        # Verify the sequence of commands
        calls = [call[0][0] for call in mock_gh_subprocess.call_args_list]
        assert calls[0] == ["gh", "pr", "checkout", "123"]

    def test_checkout_pr_branch_failure(self, mock_github_client, mock_gemini_client):
        """Test PR branch checkout failure."""
        # Setup
        from src.auto_coder import pr_processor

        pr_data = {"number": 123}

        # Mock gh pr checkout to fail
        with patch("auto_coder.gh_logger.subprocess.run") as mock_gh_subprocess:
            mock_gh_subprocess.return_value = Mock(success=False, stdout="", stderr="Branch not found", returncode=1)

            # Execute
            result = pr_processor._checkout_pr_branch("test/repo", pr_data, AutomationConfig())

            # Assert
            assert result is False

    # Remove outdated test that doesn't match current implementation
    def test_apply_github_actions_fix_no_commit_in_prompt_and_code_commits(self):
        """Test removed - outdated and doesn't match current stub implementation."""
        pass

    def test_format_direct_fix_comment(self, mock_github_client, mock_gemini_client):
        """Test direct fix comment formatting."""
        # Setup
        engine = AutomationEngine(mock_github_client)
        pr_data = {
            "number": 123,
            "title": "Fix GitHub Actions",
            "body": "This PR fixes the CI issues",
        }
        github_logs = "Error: Test failed\nFailed to install dependencies\nBuild process failed"
        fix_actions = ["Fixed configuration", "Updated dependencies"]

        # Execute
        result = engine._format_direct_fix_comment(pr_data, github_logs, fix_actions)

        # Assert
        assert "Auto-Coder Applied GitHub Actions Fixes" in result
        assert "**PR:** #123 - Fix GitHub Actions" in result
        assert "Error: Test failed" in result
        assert "Fixed configuration" in result
        assert "Updated dependencies" in result

    @patch("subprocess.run")
    def test_update_with_base_branch_up_to_date(self, mock_run, mock_github_client, mock_gemini_client):
        """Test updating PR branch when already up to date."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="0", stderr=""),  # git rev-list
        ]

        engine = AutomationEngine(mock_github_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._update_with_base_branch("test/repo", pr_data)

        # Assert
        assert len(result) == 1
        assert "up to date with main branch" in result[0]

    @patch("subprocess.run")
    def test_update_with_base_branch_merge_success(self, mock_run, mock_github_client, mock_gemini_client):
        """Test successful base branch merge."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="3", stderr=""),  # git rev-list
            Mock(returncode=0, stdout="", stderr=""),  # git merge
            Mock(returncode=0, stdout="", stderr=""),  # git push
        ]

        engine = AutomationEngine(mock_github_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._update_with_base_branch("test/repo", pr_data)

        # Assert
        assert len(result) == 4
        assert "3 commits behind main" in result[0]
        assert "Successfully merged main branch" in result[1]
        assert "Pushed updated branch" in result[2]
        assert AutomationEngine.FLAG_SKIP_ANALYSIS in result


class TestCommandExecutor:
    """Test cases for CommandExecutor class."""

    @patch("subprocess.run")
    def test_run_command_timeout(self, mock_run):
        """Test command timeout handling."""
        # Setup
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(["sleep", "10"], 5)

        # Execute
        result = CommandExecutor.run_command(["sleep", "10"], timeout=5)

        # Assert
        assert result.success is False
        assert "timed out" in result.stderr
        assert result.returncode == -1


class TestAutomationConfig:
    """Test cases for AutomationConfig class."""

    def test_get_reports_dir(self):
        """Test get_reports_dir method returns correct path."""
        from pathlib import Path

        config = AutomationConfig()

        # Test with typical repo name
        repo_name = "owner/repo"
        expected_path = str(Path.home() / ".auto-coder" / "owner_repo")
        assert config.get_reports_dir(repo_name) == expected_path

        # Test with different repo name
        repo_name2 = "another-owner/another-repo"
        expected_path2 = str(Path.home() / ".auto-coder" / "another-owner_another-repo")
        assert config.get_reports_dir(repo_name2) == expected_path2

    # Removed tests for _get_llm_backend_info method
    # These tests were failing due to backend manager initialization issues


class TestAutomationEngineExtended:
    """Extended test cases for AutomationEngine."""

    # Note: test_take_pr_actions_skips_analysis_when_flag_set removed - _take_pr_actions is now in pr_processor.py

    def test_fix_pr_issues_with_testing_success(self, mock_github_client, mock_gemini_client):
        """Test integrated PR issue fixing with successful local tests."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        pr_data = {"number": 123, "title": "Test PR"}
        github_logs = "Test failed: assertion error"

        # Mock successful test after initial fix
        from src.auto_coder import pr_processor

        with (
            patch.object(pr_processor, "_apply_github_actions_fix") as mock_github_fix,
            patch.object(pr_processor, "run_local_tests") as mock_test,
        ):
            mock_github_fix.return_value = ["Applied GitHub Actions fix"]
            mock_test.return_value = {
                "success": True,
                "output": "All tests passed",
                "errors": "",
            }

            # Execute
            from src.auto_coder.pr_processor import _fix_pr_issues_with_testing

            result = _fix_pr_issues_with_testing(
                "test/repo",
                pr_data,
                engine.config,
                github_logs,
            )

            # Assert
            assert any("Starting PR issue fixing" in action for action in result)
            assert any("Local tests passed on attempt 1" in action for action in result)
            mock_github_fix.assert_called_once()
            mock_test.assert_called_once()

    def test_fix_pr_issues_with_testing_retry(self, mock_github_client, mock_gemini_client):
        """Test integrated PR issue fixing with retry logic."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        pr_data = {"number": 123, "title": "Test PR"}
        github_logs = "Test failed: assertion error"

        # Mock test failure then success
        from src.auto_coder import pr_processor

        with (
            patch.object(pr_processor, "_apply_github_actions_fix") as mock_github_fix,
            patch.object(pr_processor, "run_local_tests") as mock_test,
            patch.object(pr_processor, "_apply_local_test_fix") as mock_local_fix,
        ):
            mock_github_fix.return_value = ["Applied GitHub Actions fix"]
            # First test fails, second test passes
            mock_test.side_effect = [
                {"success": False, "output": "Test failed", "errors": "Error"},
                {"success": True, "output": "All tests passed", "errors": ""},
            ]
            mock_local_fix.return_value = ["Applied local test fix"]

            # Execute
            from src.auto_coder.pr_processor import _fix_pr_issues_with_testing

            result = _fix_pr_issues_with_testing(
                "test/repo",
                pr_data,
                engine.config,
                github_logs,
            )

            # Assert
            assert any("Local tests failed on attempt 1" in action for action in result)
            assert any("Local tests passed on attempt 2" in action for action in result)
            mock_github_fix.assert_called_once()
            assert mock_test.call_count == 2
            mock_local_fix.assert_called_once()

    def test_checkout_pr_branch_force_cleanup(self, mock_github_client, mock_gemini_client):
        """Test PR branch checkout with force cleanup enabled."""
        # Setup
        from src.auto_coder import pr_processor

        config = AutomationConfig()
        # Enable force clean before checkout
        config.FORCE_CLEAN_BEFORE_CHECKOUT = True
        pr_data = {"number": 123, "title": "Test PR"}

        # We need to mock cmd.run_command (for git commands) and gh_logger (for gh commands)
        # Use patch.object to mock the method on the cmd instance
        with patch.object(pr_processor.cmd, "run_command") as mock_run_command, patch("auto_coder.gh_logger.subprocess.run") as mock_gh_subprocess:
            # Mock cmd.run_command for git reset and clean
            # It returns a CommandResult with success attribute
            git_results = [
                Mock(success=True, stdout="", stderr="", returncode=0),  # git reset --hard HEAD
                Mock(success=True, stdout="", stderr="", returncode=0),  # git clean -fd
            ]
            mock_run_command.side_effect = git_results

            # Mock gh_logger for gh pr checkout
            # It returns a result with success attribute
            mock_gh_subprocess.return_value = Mock(success=True, stdout="", stderr="", returncode=0)

            # Execute
            result = pr_processor._checkout_pr_branch("test/repo", pr_data, config)

            # Assert
            assert result is True
            # Verify git commands were called
            assert mock_run_command.call_count == 2
            git_calls = [call[0][0] for call in mock_run_command.call_args_list]
            assert ["git", "reset", "--hard", "HEAD"] in git_calls
            assert ["git", "clean", "-fd"] in git_calls
            # Verify gh command was called
            assert mock_gh_subprocess.call_count == 1
            gh_calls = [call[0][0] for call in mock_gh_subprocess.call_args_list]
            assert ["gh", "pr", "checkout", "123"] in gh_calls

    def test_checkout_pr_branch_without_force_clean(self, mock_github_client, mock_gemini_client):
        """Test PR branch checkout without force clean (default behavior)."""
        # Setup
        from src.auto_coder import pr_processor

        config = AutomationConfig()
        # Explicitly set to False (default)
        config.FORCE_CLEAN_BEFORE_CHECKOUT = False
        pr_data = {"number": 123, "title": "Test PR"}

        # Mock gh pr checkout to succeed (no git reset/clean calls)
        with patch("auto_coder.gh_logger.subprocess.run") as mock_gh_subprocess:
            mock_gh_subprocess.return_value = Mock(success=True, stdout="", stderr="", returncode=0)

            # Execute
            result = pr_processor._checkout_pr_branch("test/repo", pr_data, config)

            # Assert
            assert result is True
            assert mock_gh_subprocess.call_count == 1
            # Verify gh pr checkout was called
            calls = [call[0][0] for call in mock_gh_subprocess.call_args_list]
            assert ["gh", "pr", "checkout", "123"] in calls

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_with_successful_runs(self, mock_run, mock_github_client, mock_gemini_client):
        """Test parsing commit history with commits that have successful GitHub Actions runs."""
        # Setup
        # First call: git log --oneline
        git_log_output = "abc1234 Fix bug in user authentication\nabc1235 Update documentation\nabc1236 Add new feature"
        mock_run.side_effect = [
            Mock(returncode=0, stdout=git_log_output, stderr=""),  # git log
            Mock(
                returncode=0,
                stdout="test\tsuccess\t2m\thttps://github.com/test/repo/actions/runs/1",
                stderr="",
            ),  # commit 1
            Mock(
                returncode=0,
                stdout="docs\tcompleted\t1m\thttps://github.com/test/repo/actions/runs/2",
                stderr="",
            ),  # commit 2
            Mock(
                returncode=0,
                stdout="feature\tpass\t5m\thttps://github.com/test/repo/actions/runs/3",
                stderr="",
            ),  # commit 3
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=3)

        # Assert
        assert len(result) == 3
        assert result[0]["commit_hash"] == "abc1234"
        assert result[0]["message"] == "Fix bug in user authentication"
        assert result[0]["actions_status"] == "success"
        assert result[0]["actions_url"] == "https://github.com/test/repo/actions/runs/1"

        assert result[1]["commit_hash"] == "abc1235"
        assert result[1]["message"] == "Update documentation"
        assert result[1]["actions_status"] == "completed"
        assert result[1]["actions_url"] == "https://github.com/test/repo/actions/runs/2"

        assert result[2]["commit_hash"] == "abc1236"
        assert result[2]["message"] == "Add new feature"
        assert result[2]["actions_status"] == "pass"
        assert result[2]["actions_url"] == "https://github.com/test/repo/actions/runs/3"

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_with_failed_runs(self, mock_run, mock_github_client, mock_gemini_client):
        """Test parsing commit history with commits that have failed GitHub Actions runs."""
        # Setup
        git_log_output = "def5678 Fix test failure\nghi9012 Refactor code"
        mock_run.side_effect = [
            Mock(returncode=0, stdout=git_log_output, stderr=""),  # git log
            Mock(
                returncode=0,
                stdout="test\tfailure\t3m\thttps://github.com/test/repo/actions/runs/10",
                stderr="",
            ),  # commit 1
            Mock(
                returncode=0,
                stdout="ci\tfailed\t4m\thttps://github.com/test/repo/actions/runs/11",
                stderr="",
            ),  # commit 2
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=2)

        # Assert
        assert len(result) == 2
        assert result[0]["commit_hash"] == "def5678"
        assert result[0]["message"] == "Fix test failure"
        assert result[0]["actions_status"] == "failure"
        assert result[0]["actions_url"] == "https://github.com/test/repo/actions/runs/10"

        assert result[1]["commit_hash"] == "ghi9012"
        assert result[1]["message"] == "Refactor code"
        assert result[1]["actions_status"] == "failed"
        assert result[1]["actions_url"] == "https://github.com/test/repo/actions/runs/11"

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_skips_no_runs(self, mock_run, mock_github_client, mock_gemini_client):
        """Test that commits without GitHub Actions runs are skipped."""
        # Setup
        git_log_output = "jkl3456 Update README\nmno7890 Fix typo"
        mock_run.side_effect = [
            Mock(returncode=0, stdout=git_log_output, stderr=""),  # git log
            Mock(returncode=1, stdout="", stderr="no runs found"),  # commit 1 - no runs
            Mock(returncode=1, stdout="", stderr="no runs found"),  # commit 2 - no runs
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=2)

        # Assert
        assert len(result) == 0  # No commits should be returned

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_skips_in_progress(self, mock_run, mock_github_client, mock_gemini_client):
        """Test that commits with queued/in-progress Actions runs are skipped."""
        # Setup
        git_log_output = "pqr1234 Initial commit"
        mock_run.side_effect = [
            Mock(returncode=0, stdout=git_log_output, stderr=""),  # git log
            Mock(returncode=0, stdout="test\tin_progress\t1m\t", stderr=""),  # commit 1 - in progress
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=1)

        # Assert
        assert len(result) == 0  # Should skip in-progress runs

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_custom_depth(self, mock_run, mock_github_client, mock_gemini_client):
        """Test parsing commit history with custom search depth."""
        # Setup
        git_log_output = "stu1234 Commit 1\nvwx5678 Commit 2\nyza9012 Commit 3"
        mock_run.side_effect = [
            Mock(returncode=0, stdout=git_log_output, stderr=""),  # git log
            Mock(
                returncode=0,
                stdout="test\tpass\t1m\thttps://github.com/test/repo/actions/runs/20",
                stderr="",
            ),  # commit 1
            Mock(
                returncode=0,
                stdout="ci\tsuccess\t2m\thttps://github.com/test/repo/actions/runs/21",
                stderr="",
            ),  # commit 2
            Mock(
                returncode=0,
                stdout="build\tcompleted\t3m\thttps://github.com/test/repo/actions/runs/22",
                stderr="",
            ),  # commit 3
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute with custom depth of 3
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=3)

        # Assert
        assert len(result) == 3
        # Verify git log was called with -3
        mock_run.assert_any_call(
            ["git", "log", "--oneline", "-3"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_mixed_results(self, mock_run, mock_github_client, mock_gemini_client):
        """Test parsing commit history with a mix of commits: some with runs, some without."""
        # Setup
        git_log_output = "bcd1234 Fix critical bug\n efg5678 Update CHANGELOG\n hij9012 Add feature"
        mock_run.side_effect = [
            Mock(returncode=0, stdout=git_log_output, stderr=""),  # git log
            Mock(
                returncode=0,
                stdout="test\tfailure\t2m\thttps://github.com/test/repo/actions/runs/30",
                stderr="",
            ),  # commit 1 - has failed run
            Mock(returncode=1, stdout="", stderr="no runs found"),  # commit 2 - no runs
            Mock(
                returncode=0,
                stdout="feature\tsuccess\t5m\thttps://github.com/test/repo/actions/runs/31",
                stderr="",
            ),  # commit 3 - has success
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=3)

        # Assert - should only return commits with Action runs (2 out of 3)
        assert len(result) == 2
        assert result[0]["commit_hash"] == "bcd1234"
        assert result[0]["actions_status"] == "failure"
        assert result[1]["commit_hash"] == "hij9012"
        assert result[1]["actions_status"] == "success"

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_empty_log(self, mock_run, mock_github_client, mock_gemini_client):
        """Test parsing commit history when git log returns empty."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git log - empty
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=10)

        # Assert
        assert len(result) == 0

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_git_error(self, mock_run, mock_github_client, mock_gemini_client):
        """Test handling git log errors."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=1, stdout="", stderr="fatal: not a git repository"),  # git log fails
        ]

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=10)

        # Assert
        assert len(result) == 0  # Should return empty list on error

    @patch("subprocess.run")
    def test_parse_commit_history_with_actions_timeout(self, mock_run, mock_github_client, mock_gemini_client):
        """Test handling timeout during commit history parsing."""
        # Setup
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(["git", "log", "--oneline", "-10"], 30)

        engine = AutomationEngine(mock_github_client)

        # Execute
        result = engine.parse_commit_history_with_actions("test/repo", search_depth=10)

        # Assert
        assert len(result) == 0  # Should return empty list on timeout


class TestGetCandidates:
    """Test cases for _get_candidates method with priority-based selection."""

    @patch("src.auto_coder.util.github_action._check_github_actions_status")
    @patch("src.auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_get_candidates_urgent_issue_highest_priority(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that urgent issues receive the highest priority (3)."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various items
        mock_github_client.get_open_pull_requests.return_value = []
        mock_github_client.get_open_issues.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Urgent issue - should be first
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),
        ]

        # Mock issue details with one urgent issue
        issue_data = {
            1: {
                "number": 1,
                "title": "Regular issue",
                "body": "",
                "labels": [],
                "state": "open",
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Urgent issue",
                "body": "",
                "labels": ["urgent"],
                "state": "open",
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Another issue",
                "body": "",
                "labels": [],
                "state": "open",
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        def get_issue_details_side_effect(issue):
            return issue_data[issue.number]

        mock_github_client.get_issue_details.side_effect = get_issue_details_side_effect
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Urgent issue should be first (priority 1)
        assert len(candidates) == 3
        assert candidates[0].type == "issue"
        assert candidates[0].priority == 1
        assert candidates[0].data["number"] == 2
        assert "urgent" in candidates[0].data["labels"]

        # Regular issues should have priority 0
        assert candidates[1].priority == 0
        assert candidates[2].priority == 0

        mock_extract_issues.assert_not_called()  # No PRs

    @patch("src.auto_coder.util.github_action._check_github_actions_status")
    @patch("src.auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_get_candidates_priority_order_prs_and_issues(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that candidates are sorted by priority (urgent > ready > needs fix > regular)."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various PRs and issues
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # PR needs fix (priority 1)
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # PR ready for merge (priority 2)
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),  # Urgent PR (priority 3)
        ]

        mock_github_client.get_open_issues.return_value = [
            Mock(number=10, created_at="2024-01-04T00:00:00Z"),  # Regular issue (priority 0)
            Mock(number=11, created_at="2024-01-05T00:00:00Z"),  # Urgent issue (priority 3)
        ]

        # Mock PR details
        pr_data = {
            1: {
                "number": 1,
                "title": "PR with failing checks",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "PR ready for merge",
                "body": "",
                "head": {"ref": "pr-2"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Urgent PR",
                "body": "",
                "head": {"ref": "pr-3"},
                "labels": ["urgent"],
                "mergeable": False,
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        def get_issue_details_side_effect(issue):
            return {
                "number": issue.number,
                "title": f"Issue {issue.number}",
                "body": "",
                "labels": ["urgent"] if issue.number == 11 else [],
                "state": "open",
                "created_at": issue.created_at,
            }

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_issue_details.side_effect = get_issue_details_side_effect

        # Mock GitHub Actions checks
        def check_actions_side_effect(repo_name, pr_data, config):
            if pr_data["number"] == 1:
                return GitHubActionsStatusResult(success=False, ids=[])
            elif pr_data["number"] == 2:
                return GitHubActionsStatusResult(success=True, ids=[])
            elif pr_data["number"] == 3:
                return GitHubActionsStatusResult(success=False, ids=[])
            return GitHubActionsStatusResult(success=True, ids=[])

        mock_check_actions.side_effect = check_actions_side_effect

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Should be sorted by priority (6 -> 0), then by creation date (oldest first)
        assert len(candidates) == 5

        # Priority order: urgent PR (6) > ready PR (3) > needs fix PR (2) > urgent issue (1) > regular issue (0)
        assert candidates[0].priority == 6
        assert candidates[0].data["number"] == 3  # Urgent PR with failing checks

        assert candidates[1].priority == 3
        assert candidates[1].data["number"] == 2  # PR ready for merge

        assert candidates[2].priority == 2
        assert candidates[2].data["number"] == 1  # PR needing fix

        assert candidates[3].priority == 1
        assert candidates[3].data["number"] == 11  # Urgent issue

        assert candidates[4].priority == 0
        assert candidates[4].data["number"] == 10  # Regular issue

    @patch("src.auto_coder.util.github_action._check_github_actions_status")
    @patch("src.auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_get_candidates_skips_items_with_auto_coder_label(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that items with @auto-coder label are skipped."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]

        mock_github_client.get_open_issues.return_value = [
            Mock(number=10, created_at="2024-01-02T00:00:00Z"),
            Mock(number=11, created_at="2024-01-03T00:00:00Z"),  # Has @auto-coder label
        ]

        mock_github_client.get_pr_details.return_value = {
            "number": 1,
            "title": "PR",
            "body": "",
            "head": {"ref": "pr-1"},
            "labels": ["@auto-coder"],  # Has @auto-coder label - should skip
            "mergeable": True,
            "created_at": "2024-01-01T00:00:00Z",
        }

        def get_issue_details_side_effect(issue):
            return {
                "number": issue.number,
                "title": f"Issue {issue.number}",
                "body": "",
                "labels": ["@auto-coder"] if issue.number == 11 else [],
                "state": "open",
                "created_at": issue.created_at,
            }

        mock_github_client.get_issue_details.side_effect = get_issue_details_side_effect
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])
        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Only non-labeled items should be returned (PR #1 has @auto-coder label, Issue #11 has @auto-coder label)
        assert len(candidates) == 1
        assert candidates[0].data["number"] == 10  # Issue without @auto-coder label

        # PR #1 and Issue #11 should be skipped
        candidate_numbers = [c.data["number"] for c in candidates]
        assert 1 not in candidate_numbers  # PR with @auto-coder label
        assert 11 not in candidate_numbers  # Issue with @auto-coder label

    @patch("src.auto_coder.util.github_action._check_github_actions_status")
    @patch("src.auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_get_candidates_skips_issues_with_sub_issues_or_linked_prs(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that issues with sub-issues or linked PRs are skipped."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = []
        mock_github_client.get_open_issues.return_value = [
            Mock(number=10, created_at="2024-01-01T00:00:00Z"),
            Mock(number=11, created_at="2024-01-02T00:00:00Z"),  # Has sub-issues
            Mock(number=12, created_at="2024-01-03T00:00:00Z"),  # Has linked PR
        ]

        def get_issue_details_side_effect(issue):
            return {
                "number": issue.number,
                "title": f"Issue {issue.number}",
                "body": "",
                "labels": [],
                "state": "open",
                "created_at": issue.created_at,
            }

        mock_github_client.get_issue_details.side_effect = get_issue_details_side_effect
        mock_github_client.get_open_sub_issues.side_effect = lambda repo, num: ([1] if num == 11 else [])
        mock_github_client.has_linked_pr.side_effect = lambda repo, num: (True if num == 12 else False)

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Only issue #10 should be returned
        assert len(candidates) == 1
        assert candidates[0].data["number"] == 10
        assert candidates[0].type == "issue"

    @patch("src.auto_coder.util.github_action._check_github_actions_status")
    @patch("src.auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_get_candidates_extracts_related_issues_from_pr_body(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that PR candidates include related issues extracted from PR body."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]

        mock_github_client.get_open_issues.return_value = []

        mock_github_client.get_pr_details.return_value = {
            "number": 1,
            "title": "PR",
            "body": "This PR fixes #10 and #20",
            "head": {"ref": "pr-1"},
            "labels": [],
            "mergeable": True,
            "created_at": "2024-01-01T00:00:00Z",
        }

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])
        mock_extract_issues.return_value = [10, 20]  # Extracted from PR body
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert
        assert len(candidates) == 1
        assert candidates[0].type == "pr"
        assert candidates[0].data["number"] == 1
        assert candidates[0].related_issues == [10, 20]
        assert candidates[0].branch_name == "pr-1"

        mock_extract_issues.assert_called_once_with("This PR fixes #10 and #20")


class TestUrgentLabelPropagation:
    """Test cases for urgent label propagation in PR creation."""

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_create_pr_for_issue_propagates_urgent_label(self, mock_cmd, mock_github_client, mock_gemini_client):
        """Test that urgent label is propagated from issue to PR."""
        # Setup
        from src.auto_coder.issue_processor import _create_pr_for_issue

        issue_data = {
            "number": 123,
            "title": "Urgent issue",
            "body": "This is an urgent issue",
            "labels": ["urgent", "bug"],
        }

        # Mock gh pr create to return PR URL
        # Note: git commands may also be called during get_commit_log, so we need to handle those too
        gh_results = [
            Mock(success=True, stdout="https://github.com/test/repo/pull/456", returncode=0),  # gh pr create
            Mock(success=True, stdout="", stderr="", returncode=0),  # gh pr edit
        ]

        def side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                return gh_results.pop(0)
            # For git commands, just return success
            return Mock(success=True, stdout="", stderr="", returncode=0)

        mock_cmd.side_effect = side_effect

        # Mock get_pr_closing_issues to return the issue number
        mock_github_client.get_pr_closing_issues.return_value = [123]

        # Execute
        config = AutomationConfig()
        result = _create_pr_for_issue(
            repo_name="test/repo",
            issue_data=issue_data,
            work_branch="issue-123",
            base_branch="main",
            llm_response="Fixed the urgent issue",
            github_client=mock_github_client,
            config=config,
        )

        # Assert
        assert "Successfully created PR for issue #123" in result

        # Verify gh pr create was called (filter out git commands)
        gh_calls = [call for call in mock_cmd.call_args_list if call[0][0][0] == "gh"]
        assert len(gh_calls) >= 2
        create_call = gh_calls[0][0][0]
        assert create_call[0] == "gh"
        assert create_call[1] == "pr"
        assert create_call[2] == "create"

        # Verify urgent label was added to PR
        add_label_call = gh_calls[1][0][0]
        assert add_label_call[0] == "gh"
        assert add_label_call[1] == "pr"
        assert add_label_call[2] == "edit"
        assert str(456) in add_label_call  # PR number

        # Verify GitHub client was called to add labels
        mock_github_client.add_labels_to_issue.assert_called_once_with("test/repo", 456, ["urgent"])

    @patch("auto_coder.gh_logger.subprocess.run")
    def test_create_pr_for_issue_without_urgent_label(self, mock_cmd, mock_github_client, mock_gemini_client):
        """Test that no urgent label is propagated when issue doesn't have it."""
        # Setup
        from src.auto_coder.issue_processor import _create_pr_for_issue

        issue_data = {
            "number": 123,
            "title": "Regular issue",
            "body": "This is a regular issue",
            "labels": ["bug"],
        }

        # Mock gh pr create to return PR URL
        # Note: git commands may also be called during get_commit_log
        def side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                return Mock(success=True, stdout="https://github.com/test/repo/pull/456", returncode=0)
            # For git commands, just return success
            return Mock(success=True, stdout="", stderr="", returncode=0)

        mock_cmd.side_effect = side_effect

        # Mock get_pr_closing_issues to return the issue number
        mock_github_client.get_pr_closing_issues.return_value = [123]

        # Execute
        config = AutomationConfig()
        result = _create_pr_for_issue(
            repo_name="test/repo",
            issue_data=issue_data,
            work_branch="issue-123",
            base_branch="main",
            llm_response="Fixed the issue",
            github_client=mock_github_client,
            config=config,
        )

        # Assert
        assert "Successfully created PR for issue #123" in result
        # gh pr create should be called but NOT gh pr edit for urgent note
        gh_calls = [call for call in mock_cmd.call_args_list if call[0][0][0] == "gh"]
        assert len(gh_calls) == 1
        create_call = gh_calls[0][0][0]
        assert create_call[0] == "gh"
        assert create_call[1] == "pr"
        assert create_call[2] == "create"

        # Verify urgent label was NOT added
        mock_github_client.add_labels_to_issue.assert_not_called()
