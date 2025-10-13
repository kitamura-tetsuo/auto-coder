import json
import os
from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.utils import CommandExecutor


def test_create_pr_prompt_is_action_oriented_no_comments(
    mock_github_client, mock_gemini_client, sample_pr_data, test_repo_name
):
    engine = AutomationEngine(mock_github_client, mock_gemini_client)
    prompt = engine._create_pr_analysis_prompt(
        test_repo_name, sample_pr_data, pr_diff="diff..."
    )

    assert "Do NOT post any comments" in prompt
    # Should NOT ask LLM to commit/push or merge
    assert 'git commit -m "Auto-Coder: Apply fix for PR #' not in prompt
    assert "gh pr merge" not in prompt
    assert "Do NOT run git commit/push" in prompt
    assert "ACTION_SUMMARY:" in prompt
    assert "CANNOT_FIX" in prompt
    # Ensure repo/number placeholders are still present contextually
    assert str(sample_pr_data["number"]) in prompt
    assert test_repo_name in prompt


def test_apply_pr_actions_directly_does_not_post_comments(
    mock_github_client, mock_gemini_client, sample_pr_data, test_repo_name
):
    # Create a mock LLM client that returns a narrative (no ACTION_SUMMARY)
    mock_llm_client = Mock()
    mock_llm_client._run_llm_cli = Mock(
        return_value="This looks good. Thanks for the contribution! I reviewed the changes and here is my analysis."
    )

    engine = AutomationEngine(mock_github_client, mock_llm_client)

    # Stub diff generation
    with patch.object(engine, "_get_pr_diff", return_value="diff..."):
        # Ensure add_comment_to_issue is tracked
        mock_github_client.add_comment_to_issue.reset_mock()

        actions = engine._apply_pr_actions_directly(test_repo_name, sample_pr_data)

        # No comment should be posted
        mock_github_client.add_comment_to_issue.assert_not_called()
        # Actions should record LLM response in a non-commenting way
        assert any(
            a.startswith("LLM response:") or a.startswith("ACTION_SUMMARY:")
            for a in actions
        )


"""Tests for automation engine functionality."""


class TestAutomationEngine:
    """Test cases for AutomationEngine class."""

    def test_init(self, mock_github_client, mock_gemini_client, temp_reports_dir):
        """Test AutomationEngine initialization."""
        with patch("os.makedirs"):
            engine = AutomationEngine(
                mock_github_client, mock_gemini_client, dry_run=True
            )

            assert engine.github == mock_github_client
            assert engine.llm == mock_gemini_client
            assert engine.dry_run is True
            assert engine.config.REPORTS_DIR == "reports"

    @patch("src.auto_coder.automation_engine.datetime")
    def test_run_success(
        self, mock_datetime, mock_github_client, mock_gemini_client, test_repo_name
    ):
        """Test successful automation run."""
        # Setup
        mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T00:00:00"

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._process_issues = Mock(return_value=[{"issue": "processed"}])
        engine._process_pull_requests = Mock(return_value=[{"pr": "processed"}])
        engine._save_report = Mock()

        # Execute
        result = engine.run(test_repo_name)

        # Assert
        assert result["repository"] == test_repo_name
        assert result["dry_run"] is True
        assert result["issues_processed"] == [{"issue": "processed"}]
        assert result["prs_processed"] == [{"pr": "processed"}]
        assert len(result["errors"]) == 0

        engine._process_issues.assert_called_once_with(test_repo_name)
        engine._process_pull_requests.assert_called_once_with(test_repo_name)
        engine._save_report.assert_called_once()

    @patch("src.auto_coder.automation_engine.datetime")
    def test_run_jules_mode_success(
        self, mock_datetime, mock_github_client, mock_gemini_client, test_repo_name
    ):
        """Test successful run with jules mode."""
        # Setup
        mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T00:00:00"

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._process_issues_jules_mode = Mock(return_value=[{"issue": "labeled"}])
        engine._process_pull_requests = Mock(return_value=[{"pr": "processed"}])
        engine._save_report = Mock()

        # Execute
        result = engine.run(test_repo_name, jules_mode=True)

        # Assert
        assert result["repository"] == test_repo_name
        assert result["dry_run"] is True
        assert result["jules_mode"] is True
        assert result["issues_processed"] == [{"issue": "labeled"}]
        assert result["prs_processed"] == [
            {"pr": "processed"}
        ]  # PRs still processed normally
        assert len(result["errors"]) == 0

        engine._process_issues_jules_mode.assert_called_once_with(test_repo_name)
        engine._process_pull_requests.assert_called_once_with(test_repo_name)
        engine._save_report.assert_called_once()

    def test_run_with_error(
        self, mock_github_client, mock_gemini_client, test_repo_name
    ):
        """Test automation run with error."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._process_issues = Mock(side_effect=Exception("Test error"))
        engine._process_pull_requests = Mock(return_value=[])
        engine._save_report = Mock()

        # Execute
        result = engine.run(test_repo_name)

        # Assert
        assert result["repository"] == test_repo_name
        assert len(result["errors"]) == 1
        assert "Test error" in result["errors"][0]

    def test_create_feature_issues_success(
        self,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
        sample_feature_suggestion,
    ):
        """Test successful feature issues creation."""
        # Setup
        mock_issue = Mock()
        mock_issue.number = 123
        mock_issue.html_url = "https://github.com/test/repo/issues/123"

        mock_github_client.create_issue.return_value = mock_issue
        mock_gemini_client.suggest_features.return_value = [sample_feature_suggestion]

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)
        engine._get_repository_context = Mock(return_value={"name": "test-repo"})
        engine._save_report = Mock()

        # Execute
        result = engine.create_feature_issues(test_repo_name)

        # Assert
        assert len(result) == 1
        assert result[0]["number"] == 123
        assert result[0]["title"] == sample_feature_suggestion["title"]

        mock_gemini_client.suggest_features.assert_called_once()
        mock_github_client.create_issue.assert_called_once()

    def test_create_feature_issues_dry_run(
        self,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
        sample_feature_suggestion,
    ):
        """Test feature issues creation in dry run mode."""
        # Setup
        mock_gemini_client.suggest_features.return_value = [sample_feature_suggestion]

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._get_repository_context = Mock(return_value={"name": "test-repo"})
        engine._save_report = Mock()

        # Execute
        result = engine.create_feature_issues(test_repo_name)

        # Assert
        assert len(result) == 1
        assert result[0]["title"] == sample_feature_suggestion["title"]
        assert result[0]["dry_run"] is True

        mock_gemini_client.suggest_features.assert_called_once()
        mock_github_client.create_issue.assert_not_called()

    def test_process_issues_success(
        self,
        mock_github_client,
        mock_gemini_client,
        sample_issue_data,
        sample_analysis_result,
    ):
        """Test successful issues processing with single-run direct actions (no analysis phase)."""
        # Setup
        mock_issue = Mock()
        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_issue_actions = Mock(return_value=["action1", "action2"])

        # Execute
        result = engine._process_issues("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"] == sample_issue_data
        assert result[0]["analysis"] is None
        assert result[0]["solution"] is None
        assert result[0]["actions_taken"] == ["action1", "action2"]

        mock_github_client.get_open_issues.assert_called_once()

    def test_process_issues_jules_mode(self, mock_github_client):
        """Test processing issues in jules mode."""
        # Setup
        mock_issue = Mock()
        mock_issue.number = 1

        sample_issue_data = {
            "number": 1,
            "title": "Test Issue",
            "labels": ["bug"],  # No 'jules' label initially
        }

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data

        engine = AutomationEngine(
            mock_github_client, None, dry_run=False
        )  # No gemini client

        # Execute
        result = engine._process_issues_jules_mode("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"] == sample_issue_data
        assert len(result[0]["actions_taken"]) == 1
        assert "Added 'jules' label to issue #1" in result[0]["actions_taken"][0]

        mock_github_client.get_open_issues.assert_called_once()
        mock_github_client.add_labels_to_issue.assert_called_once_with(
            "test/repo", 1, ["jules"]
        )

    def test_process_issues_jules_mode_already_labeled(self, mock_github_client):
        """Test processing issues in jules mode when jules label already exists."""
        # Setup
        mock_issue = Mock()
        mock_issue.number = 1

        sample_issue_data = {
            "number": 1,
            "title": "Test Issue",
            "labels": ["bug", "jules"],  # Already has 'jules' label
        }

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data

        engine = AutomationEngine(
            mock_github_client, None, dry_run=False
        )  # No gemini client

        # Execute
        result = engine._process_issues_jules_mode("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"] == sample_issue_data
        assert len(result[0]["actions_taken"]) == 1
        assert "already has 'jules' label" in result[0]["actions_taken"][0]

        mock_github_client.get_open_issues.assert_called_once()
        # Should not call add_labels_to_issue since label already exists
        mock_github_client.add_labels_to_issue.assert_not_called()

    def test_resolve_merge_conflicts_with_gemini_model_switching(
        self, mock_github_client, mock_gemini_client
    ):
        """Test that model switching occurs during conflict resolution."""
        # Setup
        pr_data = {"number": 1, "title": "Test PR", "body": "Test PR description"}
        conflict_info = "Conflict in file.py"

        # Setup mock gemini client attributes
        mock_gemini_client.model_name = "gemini-2.5-flash"
        mock_gemini_client._run_gemini_cli.return_value = (
            "Conflicts resolved successfully"
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)

        # Execute
        result = engine._resolve_merge_conflicts_with_gemini(pr_data, conflict_info)

        # Assert
        assert (
            len(result) >= 3
        )  # Should have switch to conflict, resolution, and switch back actions

        # Verify model switching methods were called
        mock_gemini_client.switch_to_conflict_model.assert_called_once()
        mock_gemini_client.switch_to_default_model.assert_called_once()

        # Verify Gemini CLI was called
        mock_gemini_client._run_gemini_cli.assert_called_once()

        # Check that actions include model switching
        action_text = " ".join(result)
        assert "Switched to" in action_text
        assert "Switched back to" in action_text

    def test_process_issues_no_analysis_phase(
        self, mock_github_client, mock_gemini_client, sample_issue_data
    ):
        """Ensure no analysis or solution generation occurs in issue processing."""
        # Setup
        mock_issue = Mock()

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_issue_actions = Mock(return_value=[])

        # Execute
        result = engine._process_issues("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["analysis"] is None
        assert result[0]["solution"] is None

    def test_process_pull_requests_success(
        self, mock_github_client, mock_gemini_client, sample_pr_data
    ):
        """Test successful pull requests processing with single-run direct actions (no analysis phase)."""
        # Setup
        mock_pr = Mock()

        mock_github_client.get_open_pull_requests.return_value = [mock_pr]
        mock_github_client.get_pr_details.return_value = sample_pr_data

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_pr_actions = Mock(return_value=["pr_action"])

        # Execute
        result = engine._process_pull_requests("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["pr_data"] == sample_pr_data
        assert result[0]["analysis"] is None
        assert result[0]["actions_taken"] == ["pr_action"]

        mock_github_client.get_open_pull_requests.assert_called_once()

    def test_process_pull_requests_two_loop_priority(
        self, mock_github_client, mock_gemini_client
    ):
        """Test that PRs are processed in two loops: merge first, then fix."""
        # Setup - only one PR that passes Actions
        passing_pr_data = {"number": 1, "title": "Passing PR"}

        mock_pr1 = Mock()
        mock_pr1.number = 1

        mock_github_client.get_open_pull_requests.return_value = [mock_pr1]
        mock_github_client.get_pr_details.return_value = passing_pr_data

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Mock GitHub Actions status - PR passes
        engine._check_github_actions_status = Mock(return_value={"success": True})

        # Mock processing methods
        engine._process_pr_for_merge = Mock(
            return_value={
                "pr_data": passing_pr_data,
                "actions_taken": ["Successfully merged PR #1"],
                "priority": "merge",
            }
        )

        # Execute
        result = engine._process_pull_requests("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["pr_data"]["number"] == 1
        assert result[0]["priority"] == "merge"
        assert "Successfully merged" in result[0]["actions_taken"][0]

        # Verify method calls
        engine._process_pr_for_merge.assert_called_once_with(
            "test/repo", passing_pr_data
        )

    def test_process_pull_requests_failing_actions(
        self, mock_github_client, mock_gemini_client
    ):
        """Test that PRs with failing Actions are processed in second loop."""
        # Setup - only one PR that fails Actions
        failing_pr_data = {"number": 2, "title": "Failing PR"}

        mock_pr2 = Mock()
        mock_pr2.number = 2

        mock_github_client.get_open_pull_requests.return_value = [mock_pr2]
        mock_github_client.get_pr_details.return_value = failing_pr_data

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Mock GitHub Actions status - PR fails
        engine._check_github_actions_status = Mock(return_value={"success": False})

        # Mock processing methods
        engine._process_pr_for_fixes = Mock(
            return_value={
                "pr_data": failing_pr_data,
                "actions_taken": ["Fixed PR #2"],
                "priority": "fix",
            }
        )

        # Execute
        result = engine._process_pull_requests("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["pr_data"]["number"] == 2
        assert result[0]["priority"] == "fix"

        # Verify method calls
        engine._process_pr_for_fixes.assert_called_once_with(
            "test/repo", failing_pr_data
        )

    def test_process_pr_for_merge_success(
        self, mock_github_client, mock_gemini_client, sample_pr_data
    ):
        """Test processing PR for merge when Actions are passing."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)
        engine._merge_pr = Mock(return_value=True)

        # Execute
        result = engine._process_pr_for_merge("test/repo", sample_pr_data)

        # Assert
        assert result["pr_data"] == sample_pr_data
        assert result["priority"] == "merge"
        assert len(result["actions_taken"]) == 1
        assert "Successfully merged" in result["actions_taken"][0]
        engine._merge_pr.assert_called_once_with(
            "test/repo", sample_pr_data["number"], {}
        )

    def test_process_pr_for_fixes_success(
        self, mock_github_client, mock_gemini_client, sample_pr_data
    ):
        """Test processing PR for fixes when Actions are failing."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_pr_actions = Mock(return_value=["Fixed issue"])

        # Execute
        result = engine._process_pr_for_fixes("test/repo", sample_pr_data)

        # Assert
        assert result["pr_data"] == sample_pr_data
        assert result["priority"] == "fix"
        assert result["actions_taken"] == ["Fixed issue"]
        engine._take_pr_actions.assert_called_once_with("test/repo", sample_pr_data)

    def test_process_pull_requests_ignores_dependabot_when_configured(
        self, mock_github_client, mock_gemini_client
    ):
        """When IGNORE_DEPENDABOT_PRS=True, Dependabot PRs are skipped entirely."""
        # Setup PR mocks
        from unittest.mock import Mock as _Mock

        dep_pr = _Mock()
        dep_pr.number = 1
        dep_pr.user = _Mock()
        dep_pr.user.login = "dependabot[bot]"

        user_pr = _Mock()
        user_pr.number = 2
        user_pr.user = _Mock()
        user_pr.user.login = "alice"

        mock_github_client.get_open_pull_requests.return_value = [dep_pr, user_pr]
        mock_github_client.get_pr_details.return_value = {
            "number": 2,
            "title": "User PR",
        }

        # Configure engine to ignore dependabot
        from src.auto_coder.automation_engine import AutomationConfig

        cfg = AutomationConfig()
        cfg.IGNORE_DEPENDABOT_PRS = True

        engine = AutomationEngine(
            mock_github_client, mock_gemini_client, dry_run=True, config=cfg
        )
        # Force second loop path for simplicity
        engine._check_github_actions_status = _Mock(return_value={"success": False})
        engine._process_pr_for_fixes = _Mock(
            return_value={
                "pr_data": {"number": 2, "title": "User PR"},
                "actions_taken": ["Fixed PR #2"],
                "priority": "fix",
            }
        )

        result = engine._process_pull_requests("test/repo")

        # Assert only the user PR was processed
        assert len(result) == 1
        assert result[0]["pr_data"]["number"] == 2
        # Ensure we never tried to get details for the dependabot PR
        for call in mock_github_client.get_pr_details.call_args_list:
            assert call.args[0] is user_pr

    def test_process_prs_first_loop_actions_passing_and_mergeable(
        self, mock_github_client, mock_gemini_client
    ):
        """Test first loop processes PRs with passing Actions AND mergeable status."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Mock PR data - Actions passing and mergeable
        passing_mergeable_pr = Mock()
        passing_mergeable_pr.number = 1
        passing_mergeable_pr_data = {
            "number": 1,
            "title": "Passing Mergeable PR",
            "mergeable": True,
        }

        # Mock PR data - Actions passing but not mergeable
        passing_not_mergeable_pr = Mock()
        passing_not_mergeable_pr.number = 2
        passing_not_mergeable_pr_data = {
            "number": 2,
            "title": "Passing Not Mergeable PR",
            "mergeable": False,
        }

        mock_github_client.get_open_pull_requests.return_value = [
            passing_mergeable_pr,
            passing_not_mergeable_pr,
        ]
        mock_github_client.get_pr_details.side_effect = [
            passing_mergeable_pr_data,
            passing_not_mergeable_pr_data,
            passing_not_mergeable_pr_data,  # Second loop
        ]

        # Mock GitHub Actions status
        engine._check_github_actions_status = Mock(return_value={"success": True})
        engine._process_pr_for_merge = Mock(
            return_value={
                "pr_data": passing_mergeable_pr_data,
                "actions_taken": ["Successfully merged PR #1"],
                "priority": "merge",
            }
        )
        engine._process_pr_for_fixes = Mock(
            return_value={
                "pr_data": passing_not_mergeable_pr_data,
                "actions_taken": ["Fixed PR #2"],
                "priority": "fix",
            }
        )

        # Execute
        result = engine._process_pull_requests("test/repo")

        # Assert
        # Should have at least 2 results (may have error entries)
        assert len(result) >= 2

        # First PR should be processed for merge (Actions passing AND mergeable)
        engine._process_pr_for_merge.assert_called_once_with(
            "test/repo", passing_mergeable_pr_data
        )

        # Second PR should be processed for fixes in second loop (Actions passing but NOT mergeable)
        engine._process_pr_for_fixes.assert_called_once_with(
            "test/repo", passing_not_mergeable_pr_data
        )

        # Check that the first result is the merge result
        merge_result = next((r for r in result if r.get("priority") == "merge"), None)
        assert merge_result is not None
        assert merge_result["pr_data"]["number"] == 1

        # Check that the second result is the fix result
        fix_result = next((r for r in result if r.get("priority") == "fix"), None)
        assert fix_result is not None
        assert fix_result["pr_data"]["number"] == 2

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    def test_merge_pr_with_conflict_resolution_success(
        self, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """Test PR merge with successful conflict resolution."""
        # Setup
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"
        config.MAIN_BRANCH = "main"
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # Mock PR data
        pr_data = {"number": 123, "title": "Test PR", "body": "Test description"}
        mock_github_client.get_pr_details_by_number.return_value = pr_data

        # Mock merge failure due to conflicts, then success after resolution
        mock_run_command.side_effect = [
            Mock(
                success=False,
                stderr="not mergeable: the merge commit cannot be cleanly created",
            ),  # Initial merge fails
            Mock(success=True, stdout="", stderr=""),  # git reset --hard
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="", stderr=""),  # git merge --abort
            Mock(success=True, stdout="", stderr=""),  # gh pr checkout
            Mock(success=True, stdout="", stderr=""),  # git fetch
            Mock(success=True, stdout="", stderr=""),  # git merge (no conflicts)
            Mock(success=True, stdout="", stderr=""),  # git push
            Mock(success=True, stdout="Merged successfully", stderr=""),  # Retry merge
        ]

        # Mock conflict resolution
        engine._get_merge_conflict_info = Mock(return_value="")
        engine._resolve_merge_conflicts_with_gemini = Mock(
            return_value=["Resolved conflicts"]
        )

        # Execute
        result = engine._merge_pr("test/repo", 123, {})

        # Assert
        assert result is True
        assert mock_run_command.call_count == 9

        # Verify the sequence of commands
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert calls[0] == [
            "gh",
            "pr",
            "merge",
            "123",
            "--squash",
        ]  # Initial merge attempt
        assert calls[1] == ["git", "reset", "--hard"]  # Reset git state
        assert calls[2] == ["git", "clean", "-fd"]  # Clean untracked files
        assert calls[3] == ["git", "merge", "--abort"]  # Abort ongoing merge
        assert calls[4] == ["gh", "pr", "checkout", "123"]  # Checkout PR
        assert calls[5] == ["git", "fetch", "origin", "main"]  # Fetch main
        assert calls[6] == ["git", "merge", "origin/main"]  # Merge main
        assert calls[7] == ["git", "push"]  # Push changes
        assert calls[8] == ["gh", "pr", "merge", "123", "--squash"]  # Retry merge

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    def test_merge_pr_with_conflict_resolution_failure(
        self, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """Test PR merge with failed conflict resolution."""
        # Setup
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"
        config.MAIN_BRANCH = "main"
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # Mock merge failure due to conflicts, then checkout failure
        mock_run_command.side_effect = [
            Mock(
                success=False,
                stderr="not mergeable: the merge commit cannot be cleanly created",
            ),  # Initial merge fails
            Mock(success=True, stdout="", stderr=""),  # git reset --hard
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="", stderr=""),  # git merge --abort
            Mock(success=False, stderr="Failed to checkout PR"),  # Checkout fails
        ]

        # Execute
        result = engine._merge_pr("test/repo", 123, {})

        # Assert
        assert result is False
        assert mock_run_command.call_count == 5

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    def test_resolve_pr_merge_conflicts_git_cleanup(
        self, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """Test that git cleanup commands are executed before PR checkout."""
        # Setup
        config = AutomationConfig()
        config.MAIN_BRANCH = "main"
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # Mock PR data
        pr_data = {"number": 123, "title": "Test PR", "body": "Test description"}
        mock_github_client.get_pr_details_by_number.return_value = pr_data

        # Mock all commands to succeed
        mock_run_command.side_effect = [
            Mock(success=True, stdout="", stderr=""),  # git reset --hard
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="", stderr=""),  # git merge --abort
            Mock(success=True, stdout="", stderr=""),  # gh pr checkout
            Mock(success=True, stdout="", stderr=""),  # git fetch
            Mock(success=True, stdout="", stderr=""),  # git merge (no conflicts)
            Mock(success=True, stdout="", stderr=""),  # git push
        ]

        # Execute
        result = engine._resolve_pr_merge_conflicts("test/repo", 123)

        # Assert
        assert result is True
        assert mock_run_command.call_count == 7

        # Verify the sequence of commands includes git cleanup
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert calls[0] == ["git", "reset", "--hard"]  # Reset git state
        assert calls[1] == ["git", "clean", "-fd"]  # Clean untracked files
        assert calls[2] == ["git", "merge", "--abort"]  # Abort ongoing merge
        assert calls[3] == ["gh", "pr", "checkout", "123"]  # Checkout PR
        assert calls[4] == ["git", "fetch", "origin", "main"]  # Fetch main
        assert calls[5] == ["git", "merge", "origin/main"]  # Merge main
        assert calls[6] == ["git", "push"]  # Push changes

    def test_take_issue_actions_dry_run(
        self, mock_github_client, mock_gemini_client, sample_issue_data
    ):
        """Test issue actions in dry run mode."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Execute
        result = engine._take_issue_actions("test/repo", sample_issue_data)

        # Assert
        assert len(result) == 1
        assert "[DRY RUN]" in result[0]
        assert "123" in result[0]

    def test_apply_issue_actions_directly(self, mock_github_client, mock_gemini_client):
        """Test direct issue actions application using Gemini CLI."""
        # Setup
        mock_gemini_client._run_gemini_cli.return_value = "Analyzed the issue and added implementation. This is a valid bug report that has been fixed."

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        issue_data = {
            "number": 123,
            "title": "Bug in login system",
            "body": "The login system has a bug",
            "labels": ["bug"],
            "state": "open",
            "author": "testuser",
        }

        # Execute
        with patch.object(engine, "_commit_changes", return_value="Committed changes"):
            result = engine._apply_issue_actions_directly("test/repo", issue_data)

        # Assert
        assert len(result) == 3
        assert "Gemini CLI analyzed and took action" in result[0]
        assert "Added analysis comment" in result[1]
        assert "Committed changes" in result[2]

    def test_take_pr_actions_success(
        self, mock_github_client, mock_gemini_client, sample_pr_data
    ):
        """Test PR actions execution."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        assert isinstance(engine, AutomationEngine)

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    def test_resolve_pr_merge_conflicts_uses_base_branch(
        self, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """When PR base branch is not 'main', conflict resolution should fetch/merge that base branch."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # Mock PR data with non-main base
        pr_data = {
            "number": 456,
            "title": "Feature PR",
            "body": "Some changes",
            "base_branch": "develop",
        }
        mock_github_client.get_pr_details_by_number.return_value = pr_data

        # Mock all commands to succeed for conflict resolve path
        mock_run_command.side_effect = [
            Mock(success=True, stdout="", stderr=""),  # git reset --hard
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="", stderr=""),  # git merge --abort
            Mock(success=True, stdout="", stderr=""),  # gh pr checkout
            Mock(success=True, stdout="", stderr=""),  # git fetch origin develop
            Mock(success=True, stdout="", stderr=""),  # git merge origin/develop
            Mock(success=True, stdout="", stderr=""),  # git push
        ]

        # Execute
        result = engine._resolve_pr_merge_conflicts("test/repo", 456)

        # Assert
        assert result is True
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert calls[4] == ["git", "fetch", "origin", "develop"]  # Fetch base branch
        assert calls[5] == ["git", "merge", "origin/develop"]  # Merge base branch

    @patch("subprocess.run")
    def test_update_with_base_branch_uses_provided_base_branch(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """_update_with_base_branch should use pr_data.base_branch when provided (even if not main)."""
        # Setup mocks for git operations: fetch, rev-list (2 commits behind), merge, push
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="2", stderr=""),  # git rev-list
            Mock(returncode=0, stdout="", stderr=""),  # git merge
            Mock(returncode=0, stdout="", stderr=""),  # git push
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 999, "base_branch": "develop"}

        # Execute
        result = engine._update_with_base_branch("test/repo", pr_data)

        # Assert
        assert any("2 commits behind develop" in a for a in result)
        assert any(
            "Successfully merged develop branch into PR #999" in a for a in result
        )
        assert any("Pushed updated branch" in a for a in result)

    def test_get_repository_context_success(
        self, mock_github_client, mock_gemini_client
    ):
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

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._get_repository_context("test/repo")

        # Assert
        assert result["name"] == "test-repo"
        assert result["description"] == "Test description"
        assert result["language"] == "Python"
        assert result["stars"] == 100
        assert result["forks"] == 20

    def test_format_feature_issue_body(
        self, mock_github_client, mock_gemini_client, sample_feature_suggestion
    ):
        """Test feature issue body formatting."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)

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
        """Test successful report saving."""
        # Setup
        mock_join.return_value = "reports/test_report.json"
        mock_file = Mock()
        mock_open.return_value.__enter__.return_value = mock_file

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        test_data = {"test": "data"}

        # Execute
        engine._save_report(test_data, "test_report")

        # Assert
        mock_open.assert_called_once()
        mock_json_dump.assert_called_once_with(
            test_data, mock_file, indent=2, ensure_ascii=False
        )

    def test_should_auto_merge_pr_low_risk_bugfix(
        self, mock_github_client, mock_gemini_client
    ):
        """Test PR should be auto-merged for low-risk bugfix."""
        # Setup
        analysis = {
            "risk_level": "low",
            "category": "bugfix",
            "recommendations": [
                {"action": "This PR looks good and can be merged safely"}
            ],
        }
        pr_data = {"mergeable": True, "draft": False}

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is True

    def test_should_auto_merge_pr_high_risk(
        self, mock_github_client, mock_gemini_client
    ):
        """Test PR should not be auto-merged for high-risk changes."""
        # Setup
        analysis = {
            "risk_level": "high",
            "category": "bugfix",
            "recommendations": [{"action": "This PR can be merged"}],
        }
        pr_data = {"mergeable": True, "draft": False}

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

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

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is False

    @patch("subprocess.run")
    @patch("os.path.exists")
    def test_run_pr_tests_success(
        self, mock_exists, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test successful PR test execution."""
        # Setup
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=0, stdout="All tests passed", stderr="")

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
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
    def test_run_pr_tests_failure(
        self, mock_exists, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test PR test execution failure."""
        # Setup
        mock_exists.return_value = True
        mock_run.return_value = Mock(
            returncode=1, stdout="", stderr="Test failed: assertion error"
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
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
            "output": "Running tests...\nERROR: Test failed\nSome other output\nFAILED: assertion error\nMore output",
            "errors": "ImportError: module not found",
        }

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._extract_important_errors(test_result)

        # Assert
        assert "ERROR: Test failed" in result
        assert "FAILED: assertion error" in result
        assert "ImportError: module not found" in result

    @patch("subprocess.run")
    def test_check_github_actions_status_all_passed(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test GitHub Actions status check when all checks pass."""
        # Setup
        mock_run.return_value = Mock(
            returncode=0, stdout="✓ test-check\n✓ another-check"
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert result["success"] is True
        assert result["total_checks"] == 2
        assert len(result["failed_checks"]) == 0

    @patch("subprocess.run")
    def test_check_github_actions_status_some_failed(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test GitHub Actions status check when some checks fail."""
        # Setup
        mock_run.return_value = Mock(
            returncode=0, stdout="✓ passing-check\n✗ failing-check\n- pending-check"
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert result["success"] is False
        assert result["total_checks"] == 3
        assert len(result["failed_checks"]) == 2
        assert result["failed_checks"][0]["name"] == "failing-check"
        assert result["failed_checks"][0]["conclusion"] == "failure"
        assert result["failed_checks"][1]["name"] == "pending-check"
        assert result["failed_checks"][1]["conclusion"] == "pending"

    @patch("subprocess.run")
    def test_check_github_actions_status_tab_format_with_failures(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test GitHub Actions status check with tab-separated format and failures."""
        # Setup - simulating the actual output format from gh CLI
        mock_run.return_value = Mock(
            returncode=1,  # Non-zero because some checks failed
            stdout="test\tfail\t2m50s\thttps://github.com/example/repo/actions/runs/123\nformat\tpass\t27s\thttps://github.com/example/repo/actions/runs/124\nlink-pr-to-issue\tskipping\t0\thttps://github.com/example/repo/actions/runs/125",
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert result["success"] is False  # Should be False because 'test' failed
        assert result["total_checks"] == 3
        assert (
            len(result["failed_checks"]) == 1
        )  # Only 'test' failed, 'skipping' doesn't count as failure
        assert result["failed_checks"][0]["name"] == "test"
        assert result["failed_checks"][0]["conclusion"] == "failure"
        assert (
            result["failed_checks"][0]["details_url"]
            == "https://github.com/example/repo/actions/runs/123"
        )

    @patch("subprocess.run")
    def test_check_github_actions_status_tab_format_all_pass(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test GitHub Actions status check with tab-separated format and all passing."""
        # Setup
        mock_run.return_value = Mock(
            returncode=0,
            stdout="test\tpass\t2m50s\thttps://github.com/example/repo/actions/runs/123\nformat\tpass\t27s\thttps://github.com/example/repo/actions/runs/124\nlink-pr-to-issue\tskipping\t0\thttps://github.com/example/repo/actions/runs/125",
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert (
            result["success"] is True
        )  # Should be True because all required checks passed
        assert result["total_checks"] == 3
        assert len(result["failed_checks"]) == 0  # No failed checks

    @patch("subprocess.run")
    def test_check_github_actions_status_no_checks_reported(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Handle gh CLI message when no checks are reported."""
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="no checks reported on the 'feat/global-search' branch",
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        result = engine._check_github_actions_status("test/repo", pr_data)

        assert result["success"] is True
        assert result["total_checks"] == 0
        assert result["failed_checks"] == []

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    def test_checkout_pr_branch_success(
        self, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """Test successful PR branch checkout."""
        # Setup
        mock_run_command.side_effect = [
            Mock(success=True, stdout="", stderr=""),  # git reset --hard HEAD
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(
                success=True, stdout="Switched to branch", stderr=""
            ),  # gh pr checkout
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._checkout_pr_branch("test/repo", pr_data)

        # Assert
        assert result is True
        assert mock_run_command.call_count == 3

        # Verify the sequence of commands
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert calls[0] == ["git", "reset", "--hard", "HEAD"]
        assert calls[1] == ["git", "clean", "-fd"]
        assert calls[2] == ["gh", "pr", "checkout", "123"]

    @patch("subprocess.run")
    def test_checkout_pr_branch_failure(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test PR branch checkout failure."""
        # Setup
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="Branch not found")

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._checkout_pr_branch("test/repo", pr_data)

        # Assert
        assert result is False

    def test_apply_github_actions_fixes_directly(
        self, mock_github_client, mock_gemini_client
    ):
        """Test direct GitHub Actions fixes application using Gemini CLI."""
        # Setup
        mock_gemini_client._run_gemini_cli.return_value = (
            "Fixed the GitHub Actions issues by updating the test configuration"
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {
            "number": 123,
            "title": "Fix test issue",
            "body": "This PR fixes the test configuration",
        }
        github_logs = "Error: Test failed due to missing dependency"

        # Execute
        with patch.object(engine, "_commit_changes", return_value="Committed changes"):
            result = engine._apply_github_actions_fixes_directly(pr_data, github_logs)

        # Assert
        assert len(result) == 2
        assert "Gemini CLI applied GitHub Actions fixes" in result[0]
        assert "Committed changes" in result[1]

    def test_apply_local_test_fixes_directly(
        self, mock_github_client, mock_gemini_client
    ):
        """Test direct local test fixes application using Gemini CLI."""
        # Setup
        mock_gemini_client._run_gemini_cli.return_value = (
            "Fixed the local test issues by updating the import statements"
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {
            "number": 123,
            "title": "Fix import issue",
            "body": "This PR fixes the import statements",
        }
        error_summary = "ImportError: cannot import name 'helper' from 'utils'"

        # Execute
        with patch.object(engine, "_commit_changes", return_value="Committed changes"):
            result = engine._apply_local_test_fixes_directly(pr_data, error_summary)

        # Assert
        assert len(result) == 2
        assert "Gemini CLI applied local test fixes" in result[0]
        assert "Committed changes" in result[1]

    def test_apply_github_actions_fix_no_commit_in_prompt_and_code_commits(
        self, mock_github_client, mock_gemini_client
    ):
        """_apply_github_actions_fix should NOT instruct LLM to commit/push; code handles commit/push."""
        # Setup
        mock_gemini_client._run_gemini_cli.return_value = (
            "OK: changes applied and pushed"
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)
        pr_data = {
            "number": 123,
            "title": "Fix CI failures",
            "body": "This PR fixes CI issues",
        }
        github_logs = "Error: Some CI failure details"

        with patch.object(
            engine,
            "_commit_with_message",
            return_value=Mock(success=True, stdout="", stderr="", returncode=0),
        ) as mock_commit, patch.object(
            engine,
            "_push_current_branch",
            return_value=Mock(success=True, stdout="", stderr="", returncode=0),
        ) as mock_push, patch.object(
            engine.cmd,
            "run_command",
            return_value=Mock(success=True, stdout="", stderr="", returncode=0),
        ) as mock_add:
            # Execute
            result_actions = engine._apply_github_actions_fix(
                "test/repo", pr_data, github_logs
            )

        # Assert prompt has no commit/push directives
        assert mock_gemini_client._run_gemini_cli.call_count == 1
        called_prompt = mock_gemini_client._run_gemini_cli.call_args[0][0]
        assert "git commit -m" not in called_prompt
        assert "git push" not in called_prompt

        # Ensure actions contain applied fix summary and code-driven commit/push occurred
        assert any("Applied GitHub Actions fix" in a for a in result_actions)
        mock_add.assert_called()
        mock_commit.assert_called_once()
        mock_push.assert_called_once()

    def test_format_direct_fix_comment(self, mock_github_client, mock_gemini_client):
        """Test direct fix comment formatting."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {
            "number": 123,
            "title": "Fix GitHub Actions",
            "body": "This PR fixes the CI issues",
        }
        github_logs = (
            "Error: Test failed\nFailed to install dependencies\nBuild process failed"
        )
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
    def test_commit_changes_success(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test successful git commit."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        fix_suggestion = {"summary": "Fix test issue"}

        # Patch CommandExecutor to simulate successful add, scan, commit
        with patch.object(engine.cmd, "run_command") as mock_cmd:
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr="", returncode=0),  # git add .
                Mock(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git status --porcelain (no unmerged)
                Mock(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git ls-files (no files)
                Mock(
                    success=True, stdout="[commit] done", stderr="", returncode=0
                ),  # git commit
            ]
            # Execute
            result = engine._commit_changes(fix_suggestion)

        # Assert
        assert "Committed changes: Auto-Coder: Fix test issue" in result

    def test_check_github_actions_status_in_progress(
        self, mock_github_client, mock_gemini_client
    ):
        """Test GitHub Actions status check with in-progress checks."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Mock gh CLI output for in-progress checks
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stdout="test\tin_progress\t2m30s\turl1\nbuild\tpending\t1m45s\turl2",
            )

            # Execute
            result = engine._check_github_actions_status("test/repo", pr_data)

            # Assert
            assert result["success"] is False
            assert result["in_progress"] is True
            assert len(result["checks"]) == 2

    @patch("subprocess.run")
    def test_update_with_base_branch_up_to_date(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test updating PR branch when already up to date."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="0", stderr=""),  # git rev-list
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123}

        # Execute
        result = engine._update_with_base_branch("test/repo", pr_data)

        # Assert
        assert len(result) == 1
        assert "up to date with main branch" in result[0]

    @patch("subprocess.run")
    def test_update_with_base_branch_merge_success(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """Test successful base branch merge."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="3", stderr=""),  # git rev-list
            Mock(returncode=0, stdout="", stderr=""),  # git merge
            Mock(returncode=0, stdout="", stderr=""),  # git push
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
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
    def test_run_command_success(self, mock_run):
        """Test successful command execution."""
        # Setup
        mock_run.return_value = Mock(returncode=0, stdout="success", stderr="")

        # Execute
        result = CommandExecutor.run_command(["echo", "test"])

        # Assert
        assert result.success is True
        assert result.stdout == "success"
        assert result.stderr == ""
        assert result.returncode == 0

    @patch("subprocess.run")
    def test_run_command_failure(self, mock_run):
        """Test failed command execution."""
        # Setup
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="error")

        # Execute
        result = CommandExecutor.run_command(["false"])

        # Assert
        assert result.success is False
        assert result.stdout == ""
        assert result.stderr == "error"
        assert result.returncode == 1

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

    def test_auto_timeout_detection(self):
        """Test automatic timeout detection based on command type."""
        # Test git command timeout
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            CommandExecutor.run_command(["git", "status"])
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert kwargs["timeout"] == CommandExecutor.DEFAULT_TIMEOUTS["git"]


class TestAutomationConfig:
    """Test cases for AutomationConfig class."""

    def test_default_values(self):
        """Test default configuration values."""
        config = AutomationConfig()

        assert config.REPORTS_DIR == "reports"
        assert config.TEST_SCRIPT_PATH == "scripts/test.sh"
        assert config.MAX_PR_DIFF_SIZE == 2000
        assert config.MAX_PROMPT_SIZE == 1000
        assert config.MAX_RESPONSE_SIZE == 200
        assert config.MAX_FIX_ATTEMPTS == 3
        assert config.MAIN_BRANCH == "main"
        assert config.MERGE_METHOD == "--squash"
        assert config.MERGE_AUTO is True

    @patch("subprocess.run")
    def test_commit_changes_runs_dprint_on_format_failure(
        self, mock_run, mock_github_client, mock_gemini_client
    ):
        """When commit fails due to dprint formatting, run 'npx dprint fmt' and retry commit."""
        # Sequence: git add (ok), git commit (fail with dprint), npx dprint fmt (ok), git add (ok), git commit (ok)
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        # Patch CommandExecutor to simulate format failure and retry path
        with patch.object(engine.cmd, "run_command") as mock_cmd:
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr="", returncode=0),  # git add .
                Mock(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git status --porcelain
                Mock(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git ls-files (pre-commit scan)
                Mock(
                    success=False,
                    stdout="Check formatting with dprint... Failed\nFormatting issues detected. Run 'npx dprint fmt' to fix.",
                    stderr="pre-commit hook failed: dprint-format",
                    returncode=1,
                ),  # git commit fails due to dprint
                Mock(
                    success=True, stdout="Formatted 3 files", stderr="", returncode=0
                ),  # npx dprint fmt
                Mock(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git add after fmt
                Mock(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git status --porcelain
                Mock(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git ls-files (pre-commit scan again)
                Mock(
                    success=True, stdout="[commit] done", stderr="", returncode=0
                ),  # git commit success
            ]
            res = engine._commit_changes({"summary": "Fix style"})

        assert "Committed changes: Auto-Coder: Fix style" in res


class TestAutomationEngineExtended:
    """Extended test cases for AutomationEngine."""

    def test_take_pr_actions_skips_analysis_when_flag_set(
        self, mock_github_client, mock_gemini_client
    ):
        """Verify that _take_pr_actions skips LLM analysis when FLAG_SKIP_ANALYSIS is present."""
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 999, "title": "Flag Test PR"}

        with patch.object(
            engine, "_handle_pr_merge"
        ) as mock_handle_merge, patch.object(
            engine, "_apply_pr_actions_directly"
        ) as mock_apply_actions:
            mock_handle_merge.return_value = [
                "All GitHub Actions checks passed for PR #999",
                "Pushed updated branch for PR #999",
                AutomationEngine.FLAG_SKIP_ANALYSIS,
            ]

            actions = engine._take_pr_actions("test/repo", pr_data)

            assert any("skipping analysis" in a for a in actions)
            mock_apply_actions.assert_not_called()

    def test_handle_pr_merge_in_progress(self, mock_github_client, mock_gemini_client):
        """Test PR merge handling when GitHub Actions are in progress."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR"}

        # Mock GitHub Actions in progress
        with patch.object(engine, "_check_github_actions_status") as mock_check:
            mock_check.return_value = {
                "success": False,
                "in_progress": True,
                "checks": [],
            }

            # Execute
            result = engine._handle_pr_merge("test/repo", pr_data, {})

            # Assert
            assert len(result) == 1
            assert "still in progress" in result[0]
            assert "skipping to next PR" in result[0]

    def test_handle_pr_merge_success(self, mock_github_client, mock_gemini_client):
        """Test PR merge handling when GitHub Actions pass."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        pr_data = {"number": 123, "title": "Test PR"}

        # Mock GitHub Actions success
        with patch.object(engine, "_check_github_actions_status") as mock_check:
            mock_check.return_value = {
                "success": True,
                "in_progress": False,
                "checks": [],
            }

            # Execute
            result = engine._handle_pr_merge("test/repo", pr_data, {})

            # Assert
            assert len(result) == 2
            assert "All GitHub Actions checks passed" in result[0]
            assert "[DRY RUN] Would merge" in result[1]

    def test_handle_pr_merge_with_integrated_fix(
        self, mock_github_client, mock_gemini_client
    ):
        """Test PR merge handling with integrated GitHub Actions and local test fixing."""
        # Setup
        config = AutomationConfig()
        config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = (
            False  # Explicitly test main update path
        )
        engine = AutomationEngine(
            mock_github_client, mock_gemini_client, dry_run=True, config=config
        )
        pr_data = {"number": 123, "title": "Test PR"}
        failed_checks = [{"name": "test", "status": "failed"}]

        # Mock GitHub Actions failure, checkout success, and up-to-date branch
        with patch.object(
            engine, "_check_github_actions_status"
        ) as mock_check, patch.object(
            engine, "_checkout_pr_branch"
        ) as mock_checkout, patch.object(
            engine, "_update_with_base_branch"
        ) as mock_update, patch.object(
            engine, "_get_github_actions_logs"
        ) as mock_logs, patch.object(
            engine, "_fix_pr_issues_with_testing"
        ) as mock_fix:
            mock_check.return_value = {
                "success": False,
                "in_progress": False,
                "failed_checks": failed_checks,
            }
            mock_checkout.return_value = True
            mock_update.return_value = ["PR #123 is up to date with main branch"]
            mock_logs.return_value = "Test failed: assertion error"
            mock_fix.return_value = [
                "Applied GitHub Actions fix",
                "Local tests passed",
                "Committed and pushed fix",
            ]

            # Execute
            result = engine._handle_pr_merge("test/repo", pr_data, {})

            # Assert
            assert any("up to date with main branch" in action for action in result)
            assert any(
                "test failures are due to PR content" in action for action in result
            )
            mock_logs.assert_called_once_with("test/repo", failed_checks)
            mock_fix.assert_called_once_with(
                "test/repo", pr_data, "Test failed: assertion error"
            )

    def test_handle_pr_merge_skips_base_update_when_flag_true(
        self, mock_github_client, mock_gemini_client
    ):
        """When SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL is True, _handle_pr_merge should not call _update_with_base_branch and proceed to fixes."""
        # Setup engine with flag True
        config = AutomationConfig()
        config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)
        pr_data = {"number": 555, "title": "Failing PR"}

        failed_checks = [{"name": "ci", "status": "failed"}]
        with patch.object(
            engine, "_check_github_actions_status"
        ) as mock_check, patch.object(
            engine, "_checkout_pr_branch"
        ) as mock_checkout, patch.object(
            engine, "_update_with_base_branch"
        ) as mock_update, patch.object(
            engine, "_get_github_actions_logs"
        ) as mock_logs, patch.object(
            engine, "_fix_pr_issues_with_testing"
        ) as mock_fix:
            mock_check.return_value = {
                "success": False,
                "in_progress": False,
                "failed_checks": failed_checks,
            }
            mock_checkout.return_value = True
            mock_logs.return_value = "Err log"
            mock_fix.return_value = ["Applied fix", "Committed and pushed fix"]

            result = engine._handle_pr_merge("test/repo", pr_data, {})

            # Should have skipped _update_with_base_branch
            mock_update.assert_not_called()
            mock_logs.assert_called_once()
            mock_fix.assert_called_once()
            assert any("Skipping base branch update" in a for a in result)

    def test_fix_pr_issues_with_testing_success(
        self, mock_github_client, mock_gemini_client
    ):
        """Test integrated PR issue fixing with successful local tests."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        pr_data = {"number": 123, "title": "Test PR"}
        github_logs = "Test failed: assertion error"

        # Mock successful test after initial fix
        with patch.object(
            engine, "_apply_github_actions_fix"
        ) as mock_github_fix, patch.object(engine, "_run_pr_tests") as mock_test:
            mock_github_fix.return_value = ["Applied GitHub Actions fix"]
            mock_test.return_value = {
                "success": True,
                "output": "All tests passed",
                "errors": "",
            }

            # Execute
            result = engine._fix_pr_issues_with_testing(
                "test/repo", pr_data, github_logs
            )

            # Assert
            assert any("Starting PR issue fixing" in action for action in result)
            assert any("Local tests passed on attempt 1" in action for action in result)
            assert any(
                "[DRY RUN] Would commit and push fix" in action for action in result
            )
            mock_github_fix.assert_called_once()
            mock_test.assert_called_once()

    def test_fix_pr_issues_with_testing_retry(
        self, mock_github_client, mock_gemini_client
    ):
        """Test integrated PR issue fixing with retry logic."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        pr_data = {"number": 123, "title": "Test PR"}
        github_logs = "Test failed: assertion error"

        # Mock test failure then success
        with patch.object(
            engine, "_apply_github_actions_fix"
        ) as mock_github_fix, patch.object(
            engine, "_run_pr_tests"
        ) as mock_test, patch.object(
            engine, "_apply_local_test_fix"
        ) as mock_local_fix:
            mock_github_fix.return_value = ["Applied GitHub Actions fix"]
            # First test fails, second test passes
            mock_test.side_effect = [
                {"success": False, "output": "Test failed", "errors": "Error"},
                {"success": True, "output": "All tests passed", "errors": ""},
            ]
            mock_local_fix.return_value = ["Applied local test fix"]

            # Execute
            result = engine._fix_pr_issues_with_testing(
                "test/repo", pr_data, github_logs
            )

            # Assert
            assert any("Local tests failed on attempt 1" in action for action in result)
            assert any("Local tests passed on attempt 2" in action for action in result)
            mock_github_fix.assert_called_once()
            assert mock_test.call_count == 2
            mock_local_fix.assert_called_once()

    def test_checkout_pr_branch_force_cleanup(
        self, mock_github_client, mock_gemini_client
    ):
        """Test PR branch checkout with force cleanup."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR"}

        # Mock successful force cleanup and checkout
        with patch.object(engine.cmd, "run_command") as mock_cmd:
            # Mock git reset, git clean, and gh pr checkout success
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr=""),  # git reset --hard HEAD
                Mock(success=True, stdout="", stderr=""),  # git clean -fd
                Mock(success=True, stdout="", stderr=""),  # gh pr checkout
            ]

            # Execute
            result = engine._checkout_pr_branch("test/repo", pr_data)

            # Assert
            assert result is True
            assert mock_cmd.call_count == 3
            mock_cmd.assert_any_call(["git", "reset", "--hard", "HEAD"])
            mock_cmd.assert_any_call(["git", "clean", "-fd"])
            mock_cmd.assert_any_call(["gh", "pr", "checkout", "123"])

    def test_checkout_pr_branch_manual_fallback(
        self, mock_github_client, mock_gemini_client
    ):
        """Test PR branch checkout with manual fallback."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR", "head": {"ref": "feature-branch"}}

        # Mock gh pr checkout failure, then manual success
        with patch.object(engine.cmd, "run_command") as mock_cmd:
            # Mock git reset, git clean success, gh pr checkout failure, then manual success
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr=""),  # git reset --hard HEAD
                Mock(success=True, stdout="", stderr=""),  # git clean -fd
                Mock(
                    success=False, stdout="", stderr="checkout failed"
                ),  # gh pr checkout (fails)
                Mock(success=True, stdout="", stderr=""),  # git fetch (manual)
                Mock(success=True, stdout="", stderr=""),  # git checkout -B (manual)
            ]

            # Execute
            result = engine._checkout_pr_branch("test/repo", pr_data)

            # Assert
            assert result is True
            assert mock_cmd.call_count == 5
            mock_cmd.assert_any_call(
                ["git", "fetch", "origin", "pull/123/head:feature-branch"]
            )
            mock_cmd.assert_any_call(["git", "checkout", "-B", "feature-branch"])


class TestPackageLockConflictResolution:
    """Test cases for package-lock.json conflict resolution functionality."""

    def test_is_package_lock_only_conflict_true(
        self, mock_github_client, mock_gemini_client
    ):
        """Test detection of package-lock.json only conflicts."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = "UU package-lock.json\n"

        # Execute
        result = engine._is_package_lock_only_conflict(conflict_info)

        # Assert
        assert result is True

    def test_is_package_lock_only_conflict_yarn_lock(
        self, mock_github_client, mock_gemini_client
    ):
        """Test detection of yarn.lock only conflicts."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = "UU yarn.lock\n"

        # Execute
        result = engine._is_package_lock_only_conflict(conflict_info)

        # Assert
        assert result is True

    def test_is_package_lock_only_conflict_pnpm_lock(
        self, mock_github_client, mock_gemini_client
    ):
        """Test detection of pnpm-lock.yaml only conflicts."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = "UU pnpm-lock.yaml\n"

        # Execute
        result = engine._is_package_lock_only_conflict(conflict_info)

        # Assert
        assert result is True

    def test_is_package_lock_only_conflict_mixed_false(
        self, mock_github_client, mock_gemini_client
    ):
        """Test detection returns false when other files are also conflicted."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = "UU package-lock.json\nUU src/main.js\n"

        # Execute
        result = engine._is_package_lock_only_conflict(conflict_info)

        # Assert
        assert result is False

    def test_is_package_lock_only_conflict_no_conflicts(
        self, mock_github_client, mock_gemini_client
    ):
        """Test detection returns false when no conflicts exist."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = ""

        # Execute
        result = engine._is_package_lock_only_conflict(conflict_info)

        # Assert
        assert result is False

    @patch("os.path.exists")
    def test_resolve_package_lock_conflicts_npm_success(
        self, mock_exists, mock_github_client, mock_gemini_client
    ):
        """Test successful resolution of package-lock.json conflicts using npm."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR"}
        conflict_info = "UU package-lock.json\n"

        # Mock package.json exists
        mock_exists.return_value = True

        with patch.object(engine.cmd, "run_command") as mock_cmd:
            # Mock: every command returns success (covers rm, npm, add, ls-files, commit, push)
            def side_effect(cmd, timeout=None, cwd=None, check_success=True):
                return Mock(success=True, stdout="", stderr="", returncode=0)

            mock_cmd.side_effect = side_effect

            # Execute
            result = engine._resolve_package_lock_conflicts(pr_data, conflict_info)

            # Assert
            assert len(result) == 7  # Includes skip-analysis flag
            assert "Detected package-lock.json only conflicts" in result[0]
            assert "Removed conflicted file: package-lock.json" in result[1]
            assert "Successfully ran npm install" in result[2]
            assert "Staged regenerated dependency files" in result[3]
            assert "Committed resolved dependency conflicts" in result[4]
            assert (
                "Successfully pushed resolved package-lock.json conflicts" in result[5]
            )
            assert AutomationEngine.FLAG_SKIP_ANALYSIS in result

            # Verify command calls
            mock_cmd.assert_any_call(["rm", "-f", "package-lock.json"])
            mock_cmd.assert_any_call(["npm", "install"], timeout=300)
            mock_cmd.assert_any_call(["git", "add", "."])
            mock_cmd.assert_any_call(
                [
                    "git",
                    "commit",
                    "-m",
                    "Resolve package-lock.json conflicts for PR #123",
                ]
            )
            mock_cmd.assert_any_call(["git", "push"])

    @patch("os.path.exists")
    def test_resolve_package_lock_conflicts_yarn_fallback(
        self, mock_exists, mock_github_client, mock_gemini_client
    ):
        """Test resolution falls back to yarn when npm fails."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR"}
        conflict_info = "UU package-lock.json\n"

        # Mock package.json exists
        mock_exists.return_value = True

        with patch.object(engine.cmd, "run_command") as mock_cmd:
            # Mock npm failure, yarn success
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr=""),  # rm -f package-lock.json
                Mock(
                    success=False, stdout="", stderr="npm failed"
                ),  # npm install (fails)
                Mock(success=True, stdout="", stderr=""),  # yarn install (succeeds)
                Mock(success=True, stdout="", stderr=""),  # git add .
                Mock(success=True, stdout="", stderr=""),  # git commit
                Mock(success=True, stdout="", stderr=""),  # git push
            ]

            # Execute
            result = engine._resolve_package_lock_conflicts(pr_data, conflict_info)

            # Assert
            assert "Successfully ran yarn install" in result[2]

            # Verify yarn was called after npm failed
            mock_cmd.assert_any_call(["npm", "install"], timeout=300)
            mock_cmd.assert_any_call(["yarn", "install"], timeout=300)

    @patch("os.path.exists")
    def test_resolve_package_lock_conflicts_no_package_json(
        self, mock_exists, mock_github_client, mock_gemini_client
    ):
        """Test resolution when package.json doesn't exist."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR"}
        conflict_info = "UU package-lock.json\n"

        # Mock package.json doesn't exist
        mock_exists.return_value = False

        with patch.object(engine.cmd, "run_command") as mock_cmd:
            # Mock only file removal
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr="")  # rm -f package-lock.json
            ]

            # Execute
            result = engine._resolve_package_lock_conflicts(pr_data, conflict_info)

            # Assert
            assert (
                "No package.json found, skipping dependency installation" in result[2]
            )

            # Verify npm/yarn were not called
            calls = [call[0][0] for call in mock_cmd.call_args_list]
            assert "npm" not in calls
            assert "yarn" not in calls

    def test_resolve_merge_conflicts_with_gemini_package_lock_priority(
        self, mock_github_client, mock_gemini_client
    ):
        """Test that package-lock conflicts are handled with specialized resolution."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR", "body": "Test description"}
        conflict_info = "UU package-lock.json\n"

        # Mock the specialized resolution method
        with patch.object(
            engine, "_is_package_lock_only_conflict", return_value=True
        ) as mock_check:
            with patch.object(
                engine, "_resolve_package_lock_conflicts", return_value=["resolved"]
            ) as mock_resolve:
                # Execute
                result = engine._resolve_merge_conflicts_with_gemini(
                    pr_data, conflict_info
                )

                # Assert
                assert result == ["resolved"]
                mock_check.assert_called_once_with(conflict_info)
                mock_resolve.assert_called_once_with(pr_data, conflict_info)

    def test_resolve_merge_conflicts_with_gemini_normal_flow(
        self, mock_github_client, mock_gemini_client
    ):
        """Test that non-package-lock conflicts use normal Gemini resolution."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 123, "title": "Test PR", "body": "Test description"}
        conflict_info = "UU src/main.js\n"

        # Mock the check to return false (not package-lock only)
        with patch.object(engine, "_is_package_lock_only_conflict", return_value=False):
            with patch.object(engine.gemini, "switch_to_conflict_model"):
                with patch.object(engine.gemini, "switch_to_default_model"):
                    with patch.object(
                        engine.gemini,
                        "_run_gemini_cli",
                        return_value="Conflicts resolved",
                    ):
                        with patch.object(
                            engine,
                            "_commit_with_message",
                            return_value=Mock(
                                success=True, stdout="", stderr="", returncode=0
                            ),
                        ) as mock_commit, patch.object(
                            engine,
                            "_push_current_branch",
                            return_value=Mock(
                                success=True, stdout="", stderr="", returncode=0
                            ),
                        ) as mock_push, patch.object(
                            engine.cmd,
                            "run_command",
                            return_value=Mock(
                                success=True, stdout="", stderr="", returncode=0
                            ),
                        ) as mock_add:
                            # Execute
                            result = engine._resolve_merge_conflicts_with_gemini(
                                pr_data, conflict_info
                            )

                            # Assert
                            assert len(result) > 0
                            assert any("Switched to" in action for action in result)
                            assert any(
                                "Gemini CLI resolved merge conflicts" in action
                                for action in result
                            )
                            mock_add.assert_called()  # git add called
                            mock_commit.assert_called_once()
                            mock_push.assert_called_once()

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    @patch("os.path.exists")
    def test_resolve_package_lock_conflicts_monorepo(
        self, mock_exists, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """Monorepoのロックファイル競合時に、各ディレクトリでnpm/yarnを実行すること。"""

        # Setup
        # functions/package.json は存在、リポジトリルートの package.json は無し
        def exists_side_effect(path):
            return path == "functions/package.json"

        mock_exists.side_effect = exists_side_effect

        # すべてのコマンドは成功扱い
        def run_side_effect(cmd, timeout=None, cwd=None, check_success=True):
            return Mock(success=True, stdout="", stderr="", returncode=0)

        mock_run_command.side_effect = run_side_effect

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 461}
        conflict_info = "UU functions/package-lock.json\n"

        # Execute
        actions = engine._resolve_package_lock_conflicts(pr_data, conflict_info)
        assert any("package-lock.json" in action for action in actions)

        # Assert: npm/yarn install が functions ディレクトリで実行されている
        called_cmds = [
            (args[0], kwargs.get("cwd"))
            for args, kwargs in mock_run_command.call_args_list
        ]
        assert (["npm", "install"], "functions") in called_cmds or (
            ["yarn", "install"],
            "functions",
        ) in called_cmds
        # 変更のステージングとコミット、プッシュが行われる
        cmd_lists = [args[0] for args, kwargs in mock_run_command.call_args_list]
        assert ["git", "add", "."] in cmd_lists
        assert any(cmd[:2] == ["git", "commit"] for cmd in cmd_lists)
        assert ["git", "push"] in cmd_lists


class TestPackageJsonDependencyConflictResolution:
    """package.json の依存関係のみのコンフリクトに対する解決ロジックのテスト"""

    def test_is_package_json_deps_only_conflict_true(
        self, mock_github_client, mock_gemini_client
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = "UU package.json\n"

        ours_json = {"name": "app", "version": "1.0.0", "dependencies": {"a": "1.0.0"}}
        theirs_json = {
            "name": "app",
            "version": "1.0.0",
            "dependencies": {"a": "1.1.0"},
        }

        with patch.object(engine.cmd, "run_command") as mock_cmd:
            mock_cmd.side_effect = [
                Mock(success=True, stdout=json.dumps(ours_json), stderr=""),
                Mock(success=True, stdout=json.dumps(theirs_json), stderr=""),
            ]
            assert engine._is_package_json_deps_only_conflict(conflict_info) is True

    def test_is_package_json_deps_only_conflict_false_due_to_other_file(
        self, mock_github_client, mock_gemini_client
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = "UU src/index.js\n"
        assert engine._is_package_json_deps_only_conflict(conflict_info) is False

    def test_is_package_json_deps_only_conflict_false_non_dep_diff(
        self, mock_github_client, mock_gemini_client
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        conflict_info = "UU package.json\n"

        ours_json = {"name": "app", "version": "1.0.0", "dependencies": {"a": "1.0.0"}}
        theirs_json = {
            "name": "app2",
            "version": "1.0.0",
            "dependencies": {"a": "1.1.0"},
        }
        with patch.object(engine.cmd, "run_command") as mock_cmd:
            mock_cmd.side_effect = [
                Mock(success=True, stdout=json.dumps(ours_json), stderr=""),
                Mock(success=True, stdout=json.dumps(theirs_json), stderr=""),
            ]
            assert engine._is_package_json_deps_only_conflict(conflict_info) is False

    def test_resolve_package_json_dependency_conflicts_merge_and_push(
        self, mock_github_client, mock_gemini_client
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 777}
        path = "tmp_pkg/package.json"
        conflict_info = f"UU {path}\n"

        ours_json = {"name": "app", "dependencies": {"a": "1.0.0", "b": "2.0.0"}}
        theirs_json = {"name": "app", "dependencies": {"a": "1.1.0", "c": "1.0.0"}}

        try:
            with patch.object(engine.cmd, "run_command") as mock_cmd:
                # git show :2:path, :3:path, git add, git commit, git push
                mock_cmd.side_effect = [
                    Mock(success=True, stdout=json.dumps(ours_json), stderr=""),
                    Mock(success=True, stdout=json.dumps(theirs_json), stderr=""),
                    Mock(success=True, stdout="", stderr=""),
                    Mock(success=True, stdout="", stderr=""),
                    Mock(success=True, stdout="", stderr=""),
                ]

                actions = engine._resolve_package_json_dependency_conflicts(
                    pr_data, conflict_info
                )

                # 確認: ファイル内容が期待通り
                assert os.path.exists(path)
                with open(path, "r", encoding="utf-8") as f:
                    merged = json.load(f)
                assert merged["dependencies"]["a"] == "1.1.0"  # 新しい方
                assert merged["dependencies"]["b"] == "2.0.0"  # ours only
                assert merged["dependencies"]["c"] == "1.0.0"  # theirs only

                # git add に path が渡される
                add_called = any(
                    args[0] == ["git", "add", path]
                    for args, _ in mock_cmd.call_args_list
                )
                assert add_called

                # コミットとプッシュが行われる
                assert any(
                    args[0][:2] == ["git", "commit"]
                    for args, _ in mock_cmd.call_args_list
                )
                assert any(
                    args[0] == ["git", "push"] for args, _ in mock_cmd.call_args_list
                )

                # アクションにスキップフラグが含まれる
                assert AutomationEngine.FLAG_SKIP_ANALYSIS in actions
        finally:
            # 後始末
            if os.path.exists(path):
                os.remove(path)
            if os.path.exists("tmp_pkg"):
                os.rmdir("tmp_pkg")

    def test_resolve_package_json_dependency_conflicts_prefer_more_when_unknown(
        self, mock_github_client, mock_gemini_client
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 778}
        path = "tmp_pkg2/package.json"
        conflict_info = f"UU {path}\n"

        ours_json = {"name": "app", "dependencies": {"x": "*"}}  # 1件
        theirs_json = {
            "name": "app",
            "dependencies": {"x": "latest", "y": "1.0.0"},
        }  # 2件（多い）

        try:
            with patch.object(engine.cmd, "run_command") as mock_cmd:
                mock_cmd.side_effect = [
                    Mock(success=True, stdout=json.dumps(ours_json), stderr=""),
                    Mock(success=True, stdout=json.dumps(theirs_json), stderr=""),
                    Mock(success=True, stdout="", stderr=""),
                    Mock(success=True, stdout="", stderr=""),
                    Mock(success=True, stdout="", stderr=""),
                ]

                actions = engine._resolve_package_json_dependency_conflicts(
                    pr_data, conflict_info
                )

                assert os.path.exists(path)
                with open(path, "r", encoding="utf-8") as f:
                    merged = json.load(f)
                # x のバージョン比較は不可 → より多い方(theirs)を採用
                assert merged["dependencies"]["x"] == "latest"
                assert "y" in merged["dependencies"]

                assert AutomationEngine.FLAG_SKIP_ANALYSIS in actions
        finally:
            if os.path.exists(path):
                os.remove(path)
            if os.path.exists("tmp_pkg2"):
                os.rmdir("tmp_pkg2")

    def test_resolve_merge_conflicts_with_gemini_package_json_priority(
        self, mock_github_client, mock_gemini_client
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 999, "title": "Test", "body": "desc"}
        conflict_info = "UU package.json\n"

        with patch.object(
            engine, "_is_package_json_deps_only_conflict", return_value=True
        ) as mock_check:
            with patch.object(
                engine,
                "_resolve_package_json_dependency_conflicts",
                return_value=["deps-resolved"],
            ) as mock_resolve:
                result = engine._resolve_merge_conflicts_with_gemini(
                    pr_data, conflict_info
                )
                assert result == ["deps-resolved"]
                mock_check.assert_called_once_with(conflict_info)
                mock_resolve.assert_called_once_with(pr_data, conflict_info)

    def test_resolve_merge_conflicts_with_gemini_sequential_package_json_then_lock(
        self, mock_github_client, mock_gemini_client
    ):
        """Both deps-only package.json and lockfile conflicts are resolved sequentially without model switching."""
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {"number": 321, "title": "Seq", "body": "desc"}
        conflict_info = "UU package.json\nUU package-lock.json\n"

        with patch.object(
            engine,
            "_get_deps_only_conflicted_package_json_paths",
            return_value=["package.json"],
        ) as mock_paths, patch.object(
            engine,
            "_resolve_package_json_dependency_conflicts",
            return_value=["deps-merged"],
        ) as mock_pkg, patch.object(
            engine, "_resolve_package_lock_conflicts", return_value=["lock-regenerated"]
        ) as mock_lock, patch.object(
            engine.gemini, "switch_to_conflict_model"
        ) as mock_switch_fast, patch.object(
            engine.gemini, "switch_to_default_model"
        ) as mock_switch_back:
            actions = engine._resolve_merge_conflicts_with_gemini(
                pr_data, conflict_info
            )

        assert actions == ["deps-merged", "lock-regenerated"]
        mock_paths.assert_called_once()
        mock_pkg.assert_called_once()
        mock_lock.assert_called_once()
        # モデル切替は行われない（専用ルーチンのみ）
        mock_switch_fast.assert_not_called()
        mock_switch_back.assert_not_called()

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    def test_merge_pr_fallback_to_alternative_method(
        self, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """コンフリクト解消後も --squash が失敗した場合、許可されている別のマージ手法を試す。"""
        # Setup
        config = AutomationConfig()
        config.MERGE_AUTO = False  # オートマージは無効化してシンプルに
        config.MERGE_METHOD = "--squash"
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # コンフリクト解消は成功、mergeableポーリングはFalse、代替手法は --merge を許可
        engine._resolve_pr_merge_conflicts = Mock(return_value=True)
        engine._poll_pr_mergeable = Mock(return_value=False)
        engine._get_allowed_merge_methods = Mock(return_value=["--merge", "--squash"])

        # 1回目のマージ失敗、リトライ失敗、--merge で成功
        mock_run_command.side_effect = [
            Mock(
                success=False,
                stdout="",
                stderr="not mergeable: the merge commit cannot be cleanly created",
            ),  # initial
            Mock(
                success=False, stdout="", stderr="still not mergeable"
            ),  # retry after resolve
            Mock(success=True, stdout="merged", stderr=""),  # fallback --merge
        ]

        # Execute
        ok = engine._merge_pr("owner/repo", 461, {})

        # Assert
        assert ok is True
        calls = [args[0] for args, _ in mock_run_command.call_args_list]
        assert calls[0] == ["gh", "pr", "merge", "461", "--squash"]
        assert calls[-1] == ["gh", "pr", "merge", "461", "--merge"]

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    @patch("time.sleep", return_value=None)
    def test_poll_pr_mergeable(
        self, mock_sleep, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """mergeable状態のポーリングがTrueを返すこと。"""
        # Setup
        # 1回目は false、2回目で true
        mock_run_command.side_effect = [
            Mock(success=True, stdout='{"mergeable": false}', stderr=""),
            Mock(success=True, stdout='{"mergeable": true}', stderr=""),
        ]
        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        ok = engine._poll_pr_mergeable("owner/repo", 10, timeout_seconds=5, interval=1)

        # Assert
        assert ok is True
        assert mock_run_command.call_count >= 2

    @patch("src.auto_coder.automation_engine.CommandExecutor.run_command")
    def test_get_allowed_merge_methods(
        self, mock_run_command, mock_github_client, mock_gemini_client
    ):
        """リポジトリの許可されたマージ方式が正しくフラグに変換される。"""
        # Setup
        mock_run_command.return_value = Mock(
            success=True,
            stdout='{"mergeCommitAllowed": true, "rebaseMergeAllowed": false, "squashMergeAllowed": true}',
            stderr="",
        )
        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        allowed = engine._get_allowed_merge_methods("owner/repo")

        # Assert
        assert "--merge" in allowed
        assert "--squash" in allowed
        assert "--rebase" not in allowed

    def test_commit_changes_aborts_on_conflict_markers(
        self, mock_github_client, mock_gemini_client, tmp_path
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        # Create a file with conflict markers
        p = tmp_path / "conflicted.txt"
        p.write_text(
            """
<<<<<<< HEAD
foo
=======
bar
>>>>>>> branch
""",
            encoding="utf-8",
        )
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch(
                "src.auto_coder.automation_engine.CommandExecutor.run_command"
            ) as mock_run:
                # git add succeeds; commit should not be attempted when conflict markers exist
                def side_effect(cmd, timeout=None, cwd=None, check_success=True):
                    if cmd[:2] == ["git", "add"]:
                        return Mock(success=True, stdout="", stderr="", returncode=0)
                    if cmd[:2] == ["git", "ls-files"]:
                        return Mock(
                            success=True,
                            stdout="conflicted.txt\n",
                            stderr="",
                            returncode=0,
                        )
                    if cmd[:2] == ["git", "commit"]:
                        return Mock(
                            success=True,
                            stdout="SHOULD_NOT_COMMIT",
                            stderr="",
                            returncode=0,
                        )
                    return Mock(success=True, stdout="", stderr="", returncode=0)

                mock_run.side_effect = side_effect
                msg = engine._commit_changes({"summary": "Test commit"})
                assert msg.startswith("Conflict markers detected")
                # Ensure commit was not attempted
                calls = [args[0] for args, _ in mock_run.call_args_list]
                assert ["git", "commit", "-m", "Auto-Coder: Test commit"] not in calls
        finally:
            os.chdir(cwd)

    def test_commit_with_message_aborts_on_conflict_markers(
        self, mock_github_client, mock_gemini_client, tmp_path
    ):
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        p = tmp_path / "conflicted2.txt"
        p.write_text(
            """
<<<<<<< HEAD
alpha
=======
beta
>>>>>>> other
""",
            encoding="utf-8",
        )
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch(
                "src.auto_coder.automation_engine.CommandExecutor.run_command"
            ) as mock_run:

                def side_effect(cmd, timeout=None, cwd=None, check_success=True):
                    if cmd[:2] == ["git", "ls-files"]:
                        return Mock(
                            success=True,
                            stdout="conflicted2.txt\n",
                            stderr="",
                            returncode=0,
                        )
                    if cmd[:2] == ["git", "commit"]:
                        return Mock(
                            success=True,
                            stdout="SHOULD_NOT_COMMIT",
                            stderr="",
                            returncode=0,
                        )
                    return Mock(success=True, stdout="", stderr="", returncode=0)

                mock_run.side_effect = side_effect
                res = engine._commit_with_message("Test msg")
            assert res.success is False
            assert "Unresolved conflict markers" in (res.stderr or "")
        finally:
            os.chdir(cwd)
