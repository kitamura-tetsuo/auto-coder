"""Tests for parent issue processing functionality."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly, _take_issue_actions
from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt


class TestParentIssueDetection:
    """Tests for parent issue detection and branching logic."""

    def test_regular_issue_not_detected_as_parent(self):
        """Test that a regular issue without sub-issues is not detected as a parent issue."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {"number": issue_number, "title": "Regular Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # No sub-issues, no parent
        github_client.get_all_sub_issues.return_value = []
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_apply_actions.assert_called_once()
            assert "Processed issue" in result

    def test_issue_with_open_sub_issues_not_detected_as_parent(self):
        """Test that an issue with open sub-issues is not detected as a parent issue."""
        repo_name = "owner/repo"
        issue_number = 456
        issue_data = {"number": issue_number, "title": "Parent with Open Sub-Issues"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Has sub-issues, no parent, but has open sub-issues
        github_client.get_all_sub_issues.return_value = [101, 102, 103]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = [101, 102]  # Some open

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed issue with open sub-issues"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_apply_actions.assert_called_once()
            assert "Processed issue with open sub-issues" in result

    def test_child_issue_not_detected_as_parent(self):
        """Test that a child issue (has parent) is not detected as a parent issue."""
        repo_name = "owner/repo"
        issue_number = 789
        issue_data = {"number": issue_number, "title": "Child Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # No sub-issues, but has a parent
        github_client.get_all_sub_issues.return_value = []
        github_client.get_parent_issue_details.return_value = {"number": 100, "title": "Parent Issue"}
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed child issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_apply_actions.assert_called_once()
            assert "Processed child issue" in result

    def test_parent_issue_detected_correctly(self):
        """Test that a parent issue with all sub-issues closed is correctly detected and processed directly."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Has sub-issues, no parent, all sub-issues closed
        github_client.get_all_sub_issues.return_value = [101, 102, 103, 104]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []  # All closed

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Successfully processed parent issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_apply_actions.assert_called_once()
            assert "Successfully processed parent issue" in result

    def test_parent_issue_with_only_closed_sub_issues_detected(self):
        """Test that a parent issue with only closed sub-issues is detected and processed directly."""
        repo_name = "owner/repo"
        issue_number = 200
        issue_data = {"number": issue_number, "title": "Parent with All Closed"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [201, 202]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Successfully processed parent issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_apply_actions.assert_called_once()
            assert "Successfully processed parent issue" in result

    def test_github_api_errors_handled_gracefully(self):
        """Test that GitHub API errors are handled gracefully."""
        repo_name = "owner/repo"
        issue_number = 300
        issue_data = {"number": issue_number, "title": "Issue with API Error"}
        config = AutomationConfig()

        # Mock GitHub client that raises an error
        github_client = MagicMock()
        github_client.get_all_sub_issues.side_effect = Exception("GitHub API error")

        result = _take_issue_actions(repo_name, issue_data, config, github_client)

        # Should handle the error gracefully
        assert len(result) > 0
        assert f"Error processing issue #{issue_number}" in result[0]


class TestParentIssueBranchingIntegration:
    """Integration tests for parent issue detection and branching."""

    def test_detection_logic_with_multiple_conditions(self):
        """Test the complete detection logic with all conditions."""
        repo_name = "owner/repo"
        config = AutomationConfig()

        test_cases = [
            # (sub_issues, parent, open_count)
            ([101, 102], None, 0),  # Has sub-issues, no parent, all closed
            ([101, 102], None, 1),  # Has sub-issues, no parent, some open
            ([], None, 0),  # No sub-issues
            ([101, 102], {"number": 100}, 0),  # Has parent
            ([101, 102], {"number": 100}, 1),  # Has parent and open sub-issues
            ([], {"number": 100}, 0),  # Has parent, no sub-issues
        ]

        for sub_issues, parent, open_count in test_cases:
            issue_number = 100
            issue_data = {"number": issue_number, "title": "Test Issue"}

            github_client = MagicMock()
            github_client.get_all_sub_issues.return_value = sub_issues
            github_client.get_parent_issue_details.return_value = parent
            github_client.get_open_sub_issues.return_value = list(range(open_count))

            with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
                mock_apply_actions.return_value = ["Issue processed"]

                result = _take_issue_actions(repo_name, issue_data, config, github_client)

                mock_apply_actions.assert_called_once()
                assert "Issue processed" in result

    def test_empty_sub_issues_list_treated_as_no_sub_issues(self):
        """Test that an empty sub-issues list means no sub-issues."""
        repo_name = "owner/repo"
        issue_number = 150
        issue_data = {"number": issue_number, "title": "Issue with No Sub-Issues"}
        config = AutomationConfig()

        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = []
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_apply_actions.assert_called_once()
            assert "Processed" in result


class TestParentIssueDirectProcessing:
    """Tests for direct processing of parent issues with sub-issue verification prompt."""

    @patch("src.auto_coder.issue_processor.BranchManager")
    @patch("src.auto_coder.issue_processor.get_current_attempt", return_value=0)
    @patch("src.auto_coder.issue_processor.get_current_branch", return_value="main")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_parent_issue_passes_sub_issue_context_to_prompt(self, mock_cmd, mock_get_branch, mock_get_attempt, mock_branch_mgr):
        """Test that parent issues pass sub-issues summary and flags to render_prompt."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue with Sub-Issues",
            "body": "Implement overall feature architecture",
            "labels": [],
            "state": "open",
            "author": "user1",
        }
        config = AutomationConfig()

        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = None
        github_client.get_parent_issue_body.return_value = None
        github_client.get_all_sub_issues.return_value = [101, 102]

        sub_issue_1 = {"number": 101, "title": "Sub Issue 1", "state": "closed"}
        sub_issue_2 = {"number": 102, "title": "Sub Issue 2", "state": "closed"}
        github_client.get_issue.side_effect = lambda r, n: sub_issue_1 if n == 101 else sub_issue_2

        with (
            patch("src.auto_coder.issue_processor.get_commit_log", return_value="Initial commit"),
            patch("src.auto_coder.issue_processor.get_llm_backend_manager") as mock_backend,
            patch("src.auto_coder.issue_processor.commit_and_push_changes", return_value="Committed"),
            patch("src.auto_coder.issue_processor._create_pr_for_issue", return_value="Created PR"),
            patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr,
            patch("src.auto_coder.issue_processor.ProgressStage"),
            patch("src.auto_coder.issue_processor.render_prompt") as mock_render_prompt,
        ):
            mock_backend_manager = MagicMock()
            mock_backend_manager._run_llm_cli.return_value = "Verified sub-issues and implemented remaining requirements"
            mock_backend.return_value = mock_backend_manager

            mock_label_mgr.return_value.__enter__.return_value = True
            mock_label_mgr.return_value.__exit__.return_value = None

            mock_render_prompt.return_value = "Rendered action prompt"

            result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

            mock_render_prompt.assert_called_once()
            call_kwargs = mock_render_prompt.call_args.kwargs
            assert call_kwargs["has_sub_issues"] is True
            assert "Sub-issue #101: Sub Issue 1" in call_kwargs["sub_issues_summary"]
            assert "Sub-issue #102: Sub Issue 2" in call_kwargs["sub_issues_summary"]
            assert call_kwargs["main_branch"] == config.MAIN_BRANCH

    def test_render_prompt_with_has_sub_issues_block(self):
        """Test that render_prompt renders parent issue instructions when has_sub_issues is True."""
        result = render_prompt(
            "issue.action",
            repo_name="owner/repo",
            issue_number=100,
            issue_title="Parent Issue",
            issue_body="Main issue requirements",
            issue_labels="",
            issue_state="open",
            issue_author="user",
            commit_log="",
            has_sub_issues=True,
            sub_issues_summary="- Sub-issue #101: Part 1\n- Sub-issue #102: Part 2",
            main_branch="main",
        )

        assert "PARENT ISSUE CONTEXT (SUB-ISSUES MERGED TO main):" in result
        assert "Sub-issue #101: Part 1" in result
        assert "Verify that the codebase on main satisfies what the group of sub-issues was intended to implement" in result
        assert "If this main issue has any additional implementation requirements or content not covered by the sub-issues, implement them." in result
        assert "Ensure all tests pass." in result

    def test_render_prompt_without_has_sub_issues_omits_parent_context(self):
        """Test that render_prompt omits parent issue instructions when has_sub_issues is False."""
        result = render_prompt(
            "issue.action",
            repo_name="owner/repo",
            issue_number=100,
            issue_title="Regular Issue",
            issue_body="Regular issue requirements",
            issue_labels="",
            issue_state="open",
            issue_author="user",
            commit_log="",
            has_sub_issues=False,
            sub_issues_summary="",
            main_branch="main",
        )

        assert "PARENT ISSUE CONTEXT (SUB-ISSUES MERGED TO" not in result
        assert "PARENT ISSUE INSTRUCTIONS:" not in result


class TestParentIssueContextInjection:
    """Tests for parent issue context injection in sub-issues."""

    @patch("src.auto_coder.issue_processor.BranchManager")
    @patch("src.auto_coder.issue_processor.get_current_attempt", return_value=0)
    @patch("src.auto_coder.issue_processor.get_current_branch", return_value="main")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_sub_issue_with_parent_context_injected(self, mock_cmd, mock_get_branch, mock_get_attempt, mock_branch_mgr):
        """Test that sub-issues correctly inject parent issue context into prompts."""
        repo_name = "owner/repo"
        issue_number = 201
        issue_data = {
            "number": issue_number,
            "title": "Sub-issue 1: Implement feature",
            "body": "Implement the first part of the feature",
            "labels": ["bug"],
            "state": "open",
            "author": "user1",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Sub-issue has a parent
        github_client.get_parent_issue_details.return_value = {
            "number": 200,
            "title": "Parent Issue: Implement feature X",
            "state": "open",
        }
        github_client.get_parent_issue_body.return_value = "Parent issue description with full context and requirements"

        # Mock commit log
        with patch("src.auto_coder.issue_processor.get_commit_log") as mock_commit_log:
            mock_commit_log.return_value = "Initial commit"

            # Mock LLM CLI call
            with patch("src.auto_coder.issue_processor.get_llm_backend_manager") as mock_backend:
                mock_backend_manager = MagicMock()
                mock_backend_manager._run_llm_cli.return_value = "Implemented feature"
                mock_backend.return_value = mock_backend_manager

                # Mock commit_and_push_changes
                with patch("src.auto_coder.issue_processor.commit_and_push_changes") as mock_commit:
                    mock_commit.return_value = "Committed changes"

                    # Mock _create_pr_for_issue
                    with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                        mock_create_pr.return_value = "Created PR"

                        # Mock branch_context
                        with patch("src.auto_coder.issue_processor.branch_context"):
                            # Mock LabelManager
                            with patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr:
                                mock_label_mgr.return_value.__enter__.return_value = True
                                mock_label_mgr.return_value.__exit__.return_value = None

                                # Mock ProgressStage
                                with patch("src.auto_coder.issue_processor.ProgressStage"):
                                    result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

        # Verify get_parent_issue_body was called
        github_client.get_parent_issue_body.assert_called_once_with(repo_name, issue_number)

        # Verify parent context was fetched
        github_client.get_parent_issue_details.assert_called_once_with(repo_name, issue_number)

    @patch("src.auto_coder.issue_processor.BranchManager")
    @patch("src.auto_coder.issue_processor.get_current_attempt", return_value=0)
    @patch("src.auto_coder.issue_processor.get_current_branch", return_value="main")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_regular_issue_without_parent_not_affected(self, mock_cmd, mock_get_branch, mock_get_attempt, mock_branch_mgr):
        """Test that regular issues without parents do not get parent context."""
        repo_name = "owner/repo"
        issue_number = 300
        issue_data = {
            "number": issue_number,
            "title": "Regular Issue",
            "body": "A regular issue without parent",
            "labels": ["bug"],
            "state": "open",
            "author": "user2",
        }
        config = AutomationConfig()

        # Mock GitHub client - no parent
        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = None
        github_client.get_parent_issue_body.return_value = None  # Should not be called

        # Mock commit log
        with patch("src.auto_coder.issue_processor.get_commit_log") as mock_commit_log:
            mock_commit_log.return_value = "Initial commit"

            # Mock LLM CLI call
            with patch("src.auto_coder.issue_processor.get_llm_backend_manager") as mock_backend:
                mock_backend_manager = MagicMock()
                mock_backend_manager._run_llm_cli.return_value = "Implemented feature"
                mock_backend.return_value = mock_backend_manager

                # Mock commit_and_push_changes
                with patch("src.auto_coder.issue_processor.commit_and_push_changes") as mock_commit:
                    mock_commit.return_value = "Committed changes"

                    # Mock _create_pr_for_issue
                    with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                        mock_create_pr.return_value = "Created PR"

                        # Mock branch_context
                        with patch("src.auto_coder.issue_processor.branch_context"):
                            # Mock LabelManager
                            with patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr:
                                mock_label_mgr.return_value.__enter__.return_value = True
                                mock_label_mgr.return_value.__exit__.return_value = None

                                # Mock ProgressStage
                                with patch("src.auto_coder.issue_processor.ProgressStage"):
                                    result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

        # Verify parent issue methods were called but parent_issue_body returned None
        github_client.get_parent_issue_details.assert_called_once_with(repo_name, issue_number)
        # get_parent_issue_body should not be called when there's no parent
        github_client.get_parent_issue_body.assert_not_called()

    @patch("src.auto_coder.issue_processor.BranchManager")
    @patch("src.auto_coder.issue_processor.get_current_attempt", return_value=0)
    @patch("src.auto_coder.issue_processor.get_current_branch", return_value="main")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_parent_issue_body_none_handled_correctly(self, mock_cmd, mock_get_branch, mock_get_attempt, mock_branch_mgr):
        """Test that None parent issue body is handled correctly."""
        repo_name = "owner/repo"
        issue_number = 201
        issue_data = {
            "number": issue_number,
            "title": "Sub-issue",
            "body": "Body",
            "labels": [],
            "state": "open",
            "author": "user1",
        }
        config = AutomationConfig()

        # Mock GitHub client - has parent but body is None
        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = {
            "number": 200,
            "title": "Parent Issue",
            "state": "open",
        }
        github_client.get_parent_issue_body.return_value = None

        # Mock commit log
        with patch("src.auto_coder.issue_processor.get_commit_log") as mock_commit_log:
            mock_commit_log.return_value = "Initial commit"

            # Mock LLM CLI call
            with patch("src.auto_coder.issue_processor.get_llm_backend_manager") as mock_backend:
                mock_backend_manager = MagicMock()
                mock_backend_manager._run_llm_cli.return_value = "Implemented"
                mock_backend.return_value = mock_backend_manager

                # Mock commit_and_push_changes
                with patch("src.auto_coder.issue_processor.commit_and_push_changes") as mock_commit:
                    mock_commit.return_value = "Committed"

                    # Mock _create_pr_for_issue
                    with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                        mock_create_pr.return_value = "Created PR"

                        # Mock branch_context
                        with patch("src.auto_coder.issue_processor.branch_context"):
                            # Mock LabelManager
                            with patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr:
                                mock_label_mgr.return_value.__enter__.return_value = True
                                mock_label_mgr.return_value.__exit__.return_value = None

                                # Mock ProgressStage
                                with patch("src.auto_coder.issue_processor.ProgressStage"):
                                    result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

        # get_parent_issue_body should be called even if it returns None
        github_client.get_parent_issue_body.assert_called_once_with(repo_name, issue_number)

    def test_render_prompt_with_parent_issue_body(self, tmp_path):
        """Test that render_prompt correctly handles parent_issue_body parameter."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" "  action: |\n" "    Issue #$issue_number: $issue_title\n" "    Body: $issue_body\n" "    Parent Context: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test with parent_issue_body
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="123",
            issue_title="Test Issue",
            issue_body="Test body",
            parent_issue_body="Parent context here",
        )

        # Should include parent context
        assert "Parent Context: Parent context here" in result
        assert "Issue #123: Test Issue" in result

    def test_render_prompt_without_parent_issue_body(self, tmp_path):
        """Test that render_prompt works correctly when parent_issue_body is not provided."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" "  action: |\n" "    Issue #$issue_number: $issue_title\n" "    Body: $issue_body\n" "    Parent Context: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test without parent_issue_body (regular issue)
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="456",
            issue_title="Regular Issue",
            issue_body="Regular body",
        )

        # Should show empty parent context (variable not substituted)
        assert "Issue #456: Regular Issue" in result
        assert "Regular body" in result
        # When parent_issue_body is not in params, it's not substituted
        assert "$parent_issue_body" in result

    def test_render_prompt_with_empty_parent_issue_body(self, tmp_path):
        """Test that render_prompt handles empty parent_issue_body correctly."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" "  action: |\n" "    Issue #$issue_number\n" "    Parent: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test with empty string parent_issue_body
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="789",
            parent_issue_body="",
        )

        # Empty string should be substituted as empty
        assert "Parent: " in result
        assert "Issue #789" in result

    def test_render_prompt_backward_compatibility_without_parent_param(self, tmp_path):
        """Test backward compatibility - render_prompt works without parent_issue_body parameter."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Issue $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test without parent_issue_body parameter at all (backward compatibility)
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="999",
        )

        # Should work without errors
        assert "Issue 999" in result

    def test_parent_context_integration_with_labels(self, tmp_path):
        """Test that parent context works correctly with label-based prompts."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default: $issue_title"\n' "  bugfix: |\n" "    Bug Fix: $issue_title\n" "    Parent: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test with parent context and label-based prompt
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_title="Fix bug",
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            parent_issue_body="Parent bug context",
        )

        # Should use bugfix prompt with parent context
        assert "Bug Fix:" in result
        assert "Parent: Parent bug context" in result
        assert "Fix bug" in result


class TestParentIssueHighScoreCloudBackend:
    """Tests for parent issues using backend_with_high_score_cloud."""

    @patch("src.auto_coder.cli_helpers.create_high_score_cloud_backend_manager")
    @patch("src.auto_coder.issue_processor._apply_issue_actions_directly")
    def test_take_issue_actions_uses_high_score_cloud_for_parent_issue(self, mock_apply_actions, mock_create_high_score_cloud):
        """Test _take_issue_actions uses create_high_score_cloud_backend_manager for parent issues."""
        mock_backend_mgr = MagicMock()
        mock_create_high_score_cloud.return_value = mock_backend_mgr
        mock_apply_actions.return_value = ["Applied actions"]

        repo_name = "owner/repo"
        issue_data = {"number": 100, "title": "Parent Issue"}
        config = AutomationConfig()

        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [101, 102]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        result = _take_issue_actions(repo_name, issue_data, config, github_client)

        mock_create_high_score_cloud.assert_called_once()
        mock_apply_actions.assert_called_once_with(
            repo_name,
            issue_data,
            config,
            github_client,
            backend_manager=mock_backend_mgr,
        )
        assert result == ["Applied actions"]

    @patch("src.auto_coder.issue_processor.BranchManager")
    @patch("src.auto_coder.issue_processor.get_current_attempt", return_value=0)
    @patch("src.auto_coder.issue_processor.get_current_branch", return_value="main")
    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.cli_helpers.create_high_score_cloud_backend_manager")
    def test_apply_issue_actions_directly_uses_high_score_cloud_when_sub_issues_present(self, mock_create_high_score_cloud, mock_cmd, mock_get_branch, mock_get_attempt, mock_branch_mgr):
        """Test _apply_issue_actions_directly resolves high score cloud backend manager for sub-issues."""
        mock_backend_mgr = MagicMock()
        mock_backend_mgr._run_llm_cli.return_value = "Verified sub-issues and updated code"
        mock_create_high_score_cloud.return_value = mock_backend_mgr

        repo_name = "owner/repo"
        issue_data = {
            "number": 100,
            "title": "Parent Issue",
            "body": "Parent issue requirements",
            "labels": [],
            "state": "open",
            "author": "user1",
        }
        config = AutomationConfig()

        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = None
        github_client.get_parent_issue_body.return_value = None
        github_client.get_all_sub_issues.return_value = [101, 102]
        github_client.get_issue.return_value = {"number": 101, "title": "Sub Issue", "state": "closed"}

        with (
            patch("src.auto_coder.issue_processor.get_commit_log", return_value="Initial commit"),
            patch("src.auto_coder.issue_processor.commit_and_push_changes", return_value="Committed"),
            patch("src.auto_coder.issue_processor._create_pr_for_issue", return_value="Created PR"),
            patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr,
            patch("src.auto_coder.issue_processor.ProgressStage"),
            patch("src.auto_coder.issue_processor.render_prompt", return_value="Prompt content"),
        ):
            mock_label_mgr.return_value.__enter__.return_value = True
            mock_label_mgr.return_value.__exit__.return_value = None

            result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

            mock_create_high_score_cloud.assert_called_once()
            mock_backend_mgr._run_llm_cli.assert_called_once_with("Prompt content")
            assert any("LLM CLI analyzed and took action" in a for a in result)
