"""Tests for issue_processor branch creation behavior."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly, _create_pr_for_issue


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
                # Mock get_parent_issue_details to return OPEN parent
                github_client.get_parent_issue_details.return_value = {"number": parent_issue_number, "title": "Parent Issue", "state": "OPEN", "url": "http://url"}

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


def test_existing_work_branch_not_recreated():
    """Test that when work branch exists locally, it should not be recreated."""
    repo_name = "owner/repo"
    issue_number = 456
    issue_data = {"number": issue_number, "title": "Test Issue"}
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
        # Simulate: work branch already exists locally
        # git rev-parse --abbrev-ref HEAD (get current branch)
        # git rev-parse --verify work_branch (check if work branch exists)
        mock_cmd.run_command.side_effect = [
            _cmd_result(success=True, stdout="main", returncode=0),  # get current branch
            _cmd_result(success=True, stdout="issue-456", returncode=0),  # rev-parse work branch exists
        ]

        with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                # Minimal GitHub client mock
                github_client = MagicMock()
                github_client.get_parent_issue_details.return_value = None  # No parent issue

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

    # Verify that branch_context was called with create_new=False
    assert captured_calls, "branch_context was not called"
    first_args, first_kwargs = captured_calls[0]
    assert first_args[0] == "issue-456"
    assert first_kwargs.get("create_new") is False, "Work branch should not be recreated when it already exists"


def test_missing_work_branch_created_with_correct_base():
    """Test that when work branch doesn't exist, it's created from the correct base branch."""
    repo_name = "owner/repo"
    issue_number = 789
    issue_data = {"number": issue_number, "title": "Test Issue"}
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
        # Simulate: work branch does not exist locally
        # git rev-parse --abbrev-ref HEAD (get current branch)
        # git rev-parse --verify work_branch (check if work branch exists)
        mock_cmd.run_command.side_effect = [
            _cmd_result(success=True, stdout="main", returncode=0),  # get current branch
            _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse work branch missing
        ]

        with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                # Minimal GitHub client mock
                github_client = MagicMock()
                github_client.get_parent_issue_details.return_value = None  # No parent issue

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

    # Verify that branch_context was called with create_new=True and correct base_branch
    assert captured_calls, "branch_context was not called"
    first_args, first_kwargs = captured_calls[0]
    assert first_args[0] == "issue-789"
    assert first_kwargs.get("create_new") is True, "Work branch should be created when it doesn't exist"
    assert first_kwargs.get("base_branch") == config.MAIN_BRANCH, "Work branch should be created from MAIN_BRANCH when no parent issue"


class TestPRMessageGeneration:
    """Tests for PR message generation with various response formats."""

    def _create_pr_with_message_response(self, repo_name, issue_data, work_branch, base_branch, message_response, github_client, config):
        """Helper to create PR with a specific message backend response."""

        # Mock the parse function to handle various JSON formats
        def parse_response(response):
            import json
            import re

            try:
                # Try to find JSON in the response using regex (similar to parse_llm_output_as_json)
                json_pattern = r"\{.*\}|\[.*\]"
                match = re.search(json_pattern, response, re.DOTALL)

                if match:
                    json_str = match.group(0)
                else:
                    json_str = response

                # First try to parse the response directly as JSON
                parsed = json.loads(json_str)

                # If it's a list (conversation history), get the last message
                if isinstance(parsed, list) and parsed:
                    last_message = parsed[-1]
                    if isinstance(last_message, dict) and "content" in last_message:
                        content = last_message["content"]
                        # The content might be a JSON string, so try to parse it
                        try:
                            return json.loads(content)
                        except:
                            # If content is not JSON, return it as-is or as a dict
                            return {"title": content, "body": ""}

                return parsed
            except:
                # For non-JSON responses, return a default
                return {"title": "Default", "body": "Default body"}

        with patch("src.auto_coder.issue_processor.run_llm_noedit_prompt") as mock_run_prompt, patch("src.auto_coder.issue_processor.parse_llm_output_as_json", side_effect=parse_response):
            mock_run_prompt.return_value = message_response

            result = _create_pr_for_issue(
                repo_name=repo_name,
                issue_data=issue_data,
                work_branch=work_branch,
                base_branch=base_branch,
                llm_response="Test response",
                github_client=github_client,
                config=config,
            )

        return result

    def test_create_pr_with_simple_json_response(self):
        """Test PR creation with a simple JSON response."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": [],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"

        config = AutomationConfig()

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Simple JSON response
        json_response = '{"title": "Fix bug", "body": "This fixes the bug"}'

        # Mock gh pr create
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/456")
            mock_gh_logger.return_value = mock_gh_logger_instance

            result = self._create_pr_with_message_response(repo_name, issue_data, work_branch, base_branch, json_response, github_client, config)

            # Verify the PR was created
            assert "Successfully created PR" in result
            # Verify the JSON was parsed correctly
            mock_gh_logger_instance.execute_with_logging.assert_called_once()
            call_args = mock_gh_logger_instance.execute_with_logging.call_args[0][0]
            assert "--title" in call_args
            title_index = call_args.index("--title") + 1
            assert call_args[title_index] == "Fix bug"

    def test_create_pr_with_conversation_history(self):
        """Test PR creation with conversation history (list of messages)."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": [],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"

        config = AutomationConfig()

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Conversation history with prompt and system messages, followed by final JSON
        # Use json.dumps to create valid JSON
        conversation_list = [
            {"role": "user", "content": "Generate a PR message for issue #123"},
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "assistant", "content": '{"title": "Fix authentication bug", "body": "Updated authentication logic to handle edge cases with special characters in passwords"}'},
        ]
        conversation_response = json.dumps(conversation_list)

        # Mock gh pr create
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/456")
            mock_gh_logger.return_value = mock_gh_logger_instance

            result = self._create_pr_with_message_response(repo_name, issue_data, work_branch, base_branch, conversation_response, github_client, config)

            # Verify the PR was created
            assert "Successfully created PR" in result
            # Verify the JSON from the last message was parsed correctly
            mock_gh_logger_instance.execute_with_logging.assert_called_once()
            call_args = mock_gh_logger_instance.execute_with_logging.call_args[0][0]
            assert "--title" in call_args
            title_index = call_args.index("--title") + 1
            assert call_args[title_index] == "Fix authentication bug"

    def test_create_pr_with_text_before_json(self):
        """Test PR creation with text before JSON response."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": [],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"

        config = AutomationConfig()

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Response with text before JSON
        response_with_text = 'Here is the PR message:\n{"title": "Update docs", "body": "Updated documentation"}'

        # Mock gh pr create
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/456")
            mock_gh_logger.return_value = mock_gh_logger_instance

            result = self._create_pr_with_message_response(repo_name, issue_data, work_branch, base_branch, response_with_text, github_client, config)

            # Verify the PR was created
            assert "Successfully created PR" in result
            # Verify the JSON was extracted and parsed correctly
            mock_gh_logger_instance.execute_with_logging.assert_called_once()
            call_args = mock_gh_logger_instance.execute_with_logging.call_args[0][0]
            assert "--title" in call_args
            title_index = call_args.index("--title") + 1
            assert call_args[title_index] == "Update docs"


class TestPRLabelCopying:
    """Integration tests for PR label copying functionality."""

    def _create_pr_with_json_message(self, repo_name, issue_data, work_branch, base_branch, pr_title, pr_body, github_client, config):
        """Helper to create PR with mocked JSON message backend."""
        # Create JSON response
        json_response = '{"title": ' + f'"{pr_title}"' + ', "body": ' + f'"{pr_body}"' + "}"

        with patch("src.auto_coder.issue_processor.run_llm_noedit_prompt") as mock_run_prompt, patch("src.auto_coder.issue_processor.parse_llm_output_as_json", return_value={"title": pr_title, "body": pr_body}):
            mock_run_prompt.return_value = json_response

            result = _create_pr_for_issue(
                repo_name=repo_name,
                issue_data=issue_data,
                work_branch=work_branch,
                base_branch=base_branch,
                llm_response="Test response",
                github_client=github_client,
                config=config,
            )

        return result

    def test_create_pr_for_issue_copies_semantic_labels(self):
        """Test that PR creation copies semantic labels from issue to PR."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug", "urgent", "documentation"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue with JSON message
            result = self._create_pr_with_json_message(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                "Test PR Title",
                "Test PR body with issue details",
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Verify label copying - only urgent label should be propagated
            # Now using generic add_labels method with "pr"
            label_calls = []
            for call in github_client.method_calls:
                if call[0] == "add_labels":
                    label_calls.append(call)

            # Should have at least one call for urgent
            assert len(label_calls) >= 1
            # Verify the label propagated is 'urgent' with "pr"
            assert any(call[0] == "add_labels" and call[1][0] == repo_name and call[1][1] == pr_number and call[1][2] == ["urgent"] and call[2] == {"item_type": "pr"} for call in label_calls)

    def test_create_pr_for_issue_copies_labels_with_aliases(self):
        """Test that PR creation handles label aliases correctly."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bugfix", "high-priority", "doc"],  # Aliases
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue with JSON message
            result = self._create_pr_with_json_message(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                "Test PR Title",
                "Test PR body with issue details",
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should have label calls for urgent (from high-priority) - only urgent label is propagated
            label_calls = []
            for call in github_client.method_calls:
                if call[0] == "add_labels":
                    label_calls.append(call)

            # Should have at least one call for urgent
            assert len(label_calls) >= 1
            # Verify the label propagated is 'urgent' with "pr"
            assert any(call[0] == "add_labels" and call[1][0] == repo_name and call[1][1] == pr_number and call[1][2] == ["urgent"] and call[2] == {"item_type": "pr"} for call in label_calls)

    def test_create_pr_for_issue_respects_max_label_count(self):
        """Test that PR creation respects the maximum label count."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug", "urgent", "enhancement", "documentation", "breaking-change"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True
        config.PR_LABEL_MAX_COUNT = 2  # Limit to 2 labels

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue with JSON message
            result = self._create_pr_with_json_message(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                "Test PR Title",
                "Test PR body with issue details",
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should only copy urgent label (only urgent is propagated)
            label_calls = []
            for call in github_client.method_calls:
                if call[0] == "add_labels":
                    label_calls.append(call)

            # At least 1 label addition for urgent
            assert len(label_calls) >= 1
            # Verify the label propagated is 'urgent' with "pr"
            assert any(call[0] == "add_labels" and call[1][0] == repo_name and call[1][1] == pr_number and call[1][2] == ["urgent"] and call[2] == {"item_type": "pr"} for call in label_calls)

    def test_create_pr_for_issue_disabled_label_copying(self):
        """Test that PR creation skips label copying when disabled."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug", "urgent"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = False  # Disabled

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue with JSON message
            result = self._create_pr_with_json_message(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                "Test PR Title",
                "Test PR body with issue details",
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should not have any label-related calls
            label_calls = []
            for call in github_client.method_calls:
                if call[0] == "add_labels":
                    label_calls.append(call)

            assert len(label_calls) == 0

    def test_create_pr_for_issue_no_semantic_labels(self):
        """Test that PR creation handles issues with no semantic labels."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["random-label", "another-label"],  # No semantic labels
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue with JSON message
            result = self._create_pr_with_json_message(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                "Test PR Title",
                "Test PR body with issue details",
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should not have any label-related calls (no semantic labels to copy)
            label_calls = []
            for call in github_client.method_calls:
                if call[0] == "add_labels":
                    label_calls.append(call)

            assert len(label_calls) == 0

    def test_create_pr_for_issue_handles_label_error_gracefully(self):
        """Test that PR creation continues even if label copying fails."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client that raises error on label operations
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]
        github_client.add_labels.side_effect = Exception("GitHub API error")

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue - should not raise despite label error
            result = self._create_pr_with_json_message(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                "Test PR Title",
                "Test PR body with issue details",
                github_client,
                config,
            )

            # Verify PR was still created successfully
            assert f"Successfully created PR for issue #{issue_number}" in result


class TestLabelBasedIssueProcessing:
    """Test label-based issue processing functionality."""

    def test_breaking_change_issue_detection(self):
        """Test detection and processing of breaking-change labeled issues."""
        from src.auto_coder.prompt_loader import _is_breaking_change_issue

        # Test breaking-change label detection
        issue_labels = ["breaking-change", "bug"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test api-change label
        issue_labels = ["api-change", "enhancement"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test deprecation label
        issue_labels = ["deprecation", "documentation"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test version-major label
        issue_labels = ["version-major", "feature"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test non-breaking labels
        issue_labels = ["bug", "enhancement", "documentation"]
        assert _is_breaking_change_issue(issue_labels) is False

    def test_label_based_prompt_selection_in_issue_processing(self):
        """Test label-based prompt selection in issue processing."""
        from src.auto_coder.prompt_loader import get_label_specific_prompt

        # Test urgent label priority
        labels = ["urgent", "bug", "feature"]
        mappings = {
            "urgent": "issue.urgent",
            "bug": "issue.bug",
            "feature": "issue.feature",
        }
        priorities = ["urgent", "bug", "feature"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.urgent"

        # Test bug label handling
        labels = ["bug", "feature"]
        priorities = ["urgent", "bug", "feature"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.bug"

        # Test feature label handling
        labels = ["feature", "documentation"]
        priorities = ["urgent", "bug", "feature", "documentation"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.feature"

        # Test priority resolution with multiple labels
        labels = ["urgent", "breaking-change"]
        mappings["breaking-change"] = "issue.breaking_change"
        priorities = ["breaking-change", "urgent", "bug", "feature"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.breaking_change"

    def test_issue_processing_with_different_label_types(self, tmp_path):
        """Test issue processing with different label types."""
        from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt

        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default: $issue_number"\n' '  bug: "Bug fix: $issue_number"\n' '  feature: "Feature: $issue_number"\n' '  breaking_change: "Breaking: $issue_number"\n' '  urgent: "Urgent: $issue_number"\n' '  documentation: "Docs: $issue_number"\n',
            encoding="utf-8",
        )

        # Test bug label
        clear_prompt_cache()
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="123",
        )
        assert "Bug fix: 123" in result

        # Test feature label
        clear_prompt_cache()
        labels = ["feature"]
        mappings = {"feature": "issue.feature"}
        priorities = ["feature"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="456",
        )
        assert "Feature: 456" in result

        # Test urgent label
        clear_prompt_cache()
        labels = ["urgent"]
        mappings = {"urgent": "issue.urgent"}
        priorities = ["urgent"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="789",
        )
        assert "Urgent: 789" in result

        # Test breaking-change label
        clear_prompt_cache()
        labels = ["breaking-change"]
        mappings = {"breaking-change": "issue.breaking_change"}
        priorities = ["breaking-change"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="999",
        )
        assert "Breaking: 999" in result

    def test_label_processing_error_handling(self):
        """Test error handling in label-based processing."""
        from src.auto_coder.prompt_loader import get_label_specific_prompt

        # Test missing label-specific prompts
        labels = ["custom-label"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result is None

        # Test invalid label configurations - should handle gracefully
        labels = []
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result is None

        # Test None inputs
        result = get_label_specific_prompt(None, None, None)
        assert result is None

    def test_issue_with_multiple_labels_uses_highest_priority(self, tmp_path):
        """Test that issue with multiple labels uses the highest priority label."""
        from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt

        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default"\n' '  bug: "Bug prompt"\n' '  feature: "Feature prompt"\n' '  urgent: "Urgent prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["bug", "feature", "urgent"]
        mappings = {
            "bug": "issue.bug",
            "feature": "issue.feature",
            "urgent": "issue.urgent",
        }
        priorities = ["urgent", "bug", "feature"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
        )

        # Urgent should be selected (highest priority)
        assert "Urgent prompt" in result

    def test_issue_without_labels_falls_back_to_default(self, tmp_path):
        """Test that issue without labels falls back to default prompt."""
        from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt

        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = []
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
        )

        # Should fall back to default
        assert "Default prompt" in result

    def test_case_insensitive_label_matching(self):
        """Test that label matching is case-insensitive."""
        from src.auto_coder.prompt_loader import _is_breaking_change_issue

        # Test breaking-change detection with different cases
        assert _is_breaking_change_issue(["BREAKING-CHANGE"]) is True
        assert _is_breaking_change_issue(["Breaking-Change"]) is True
        assert _is_breaking_change_issue(["breaking-change"]) is True

        # Test that non-breaking labels are correctly identified
        assert _is_breaking_change_issue(["bug"]) is False
        assert _is_breaking_change_issue(["FEATURE"]) is False


class TestKeepLabelOnPRCreation:
    """Test that keep_label() is called on successful PR creation."""

    def test_apply_issue_actions_calls_keep_label_on_successful_pr(self):
        """Test that _apply_issue_actions_directly calls keep_label when PR is successfully created."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {"number": issue_number, "title": "Test Issue", "body": "Test body"}
        config = AutomationConfig()

        # Track if keep_label was called
        keep_label_called = []

        @contextmanager
        def fake_branch_context(*args, **kwargs):
            yield

        # Create a mock LabelManagerContext that tracks keep_label calls
        class MockLabelManagerContext:
            def __init__(self, should_process):
                self._should_process = should_process

            def __bool__(self):
                return self._should_process

            def keep_label(self):
                keep_label_called.append(True)

        @contextmanager
        def fake_label_manager(*_args, **_kwargs):
            yield MockLabelManagerContext(True)

        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            # Simulate: work branch does not exist locally
            mock_cmd.run_command.side_effect = [
                _cmd_result(success=True, stdout="main", returncode=0),  # get current branch
                _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse work branch missing
            ]

            with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
                with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                    with patch("src.auto_coder.issue_processor.get_commit_log", return_value=""):
                        with patch("src.auto_coder.issue_processor.commit_and_push_changes", return_value="Committed"):
                            # Mock _create_pr_for_issue to return success message
                            with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                                mock_create_pr.return_value = f"Successfully created PR for issue #{issue_number}: Test PR"

                                # Mock GitHub client
                                github_client = MagicMock()
                                github_client.get_parent_issue_details.return_value = None
                                github_client.get_all_sub_issues.return_value = []

                                # Mock LLM response
                                class DummyLLM:
                                    def _run_llm_cli(self, *_args, **_kwargs):
                                        return "Made some changes to fix the issue"

                                with patch("src.auto_coder.issue_processor.get_llm_backend_manager", return_value=DummyLLM()):
                                    _apply_issue_actions_directly(
                                        repo_name,
                                        issue_data,
                                        config,
                                        github_client,
                                    )

        # Verify keep_label was called
        assert len(keep_label_called) == 1, "keep_label should be called once on successful PR creation"

    def test_apply_issue_actions_does_not_call_keep_label_on_failed_pr(self):
        """Test that _apply_issue_actions_directly does not call keep_label when PR creation fails."""
        repo_name = "owner/repo"
        issue_number = 456
        issue_data = {"number": issue_number, "title": "Test Issue", "body": "Test body"}
        config = AutomationConfig()

        # Track if keep_label was called
        keep_label_called = []

        @contextmanager
        def fake_branch_context(*args, **kwargs):
            yield

        # Create a mock LabelManagerContext that tracks keep_label calls
        class MockLabelManagerContext:
            def __init__(self, should_process):
                self._should_process = should_process

            def __bool__(self):
                return self._should_process

            def keep_label(self):
                keep_label_called.append(True)

        @contextmanager
        def fake_label_manager(*_args, **_kwargs):
            yield MockLabelManagerContext(True)

        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            # Simulate: work branch does not exist locally
            mock_cmd.run_command.side_effect = [
                _cmd_result(success=True, stdout="main", returncode=0),  # get current branch
                _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse work branch missing
            ]

            with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
                with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                    with patch("src.auto_coder.issue_processor.get_commit_log", return_value=""):
                        with patch("src.auto_coder.issue_processor.commit_and_push_changes", return_value="Committed"):
                            # Mock _create_pr_for_issue to return failure message
                            with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                                mock_create_pr.return_value = f"Failed to create PR for issue #{issue_number}: Error"

                                # Mock GitHub client
                                github_client = MagicMock()
                                github_client.get_parent_issue_details.return_value = None
                                github_client.get_all_sub_issues.return_value = []

                                # Mock LLM response
                                class DummyLLM:
                                    def _run_llm_cli(self, *_args, **_kwargs):
                                        return "Made some changes to fix the issue"

                                with patch("src.auto_coder.issue_processor.get_llm_backend_manager", return_value=DummyLLM()):
                                    _apply_issue_actions_directly(
                                        repo_name,
                                        issue_data,
                                        config,
                                        github_client,
                                    )

        # Verify keep_label was NOT called
        assert len(keep_label_called) == 0, "keep_label should not be called when PR creation fails"

    def test_apply_issue_actions_does_not_call_keep_label_for_pr_item(self):
        """Test that _apply_issue_actions_directly does not call keep_label for PR items (they have head_branch)."""
        repo_name = "owner/repo"
        issue_number = 789
        # PR items have head_branch set
        issue_data = {"number": issue_number, "title": "Test PR", "body": "Test body", "head_branch": "feature-branch"}
        config = AutomationConfig()

        # Track if keep_label was called
        keep_label_called = []

        @contextmanager
        def fake_branch_context(*args, **kwargs):
            yield

        # Create a mock LabelManagerContext that tracks keep_label calls
        class MockLabelManagerContext:
            def __init__(self, should_process):
                self._should_process = should_process

            def __bool__(self):
                return self._should_process

            def keep_label(self):
                keep_label_called.append(True)

        @contextmanager
        def fake_label_manager(*_args, **_kwargs):
            yield MockLabelManagerContext(True)

        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            # Simulate PR branch exists
            mock_cmd.run_command.side_effect = [
                _cmd_result(success=True, stdout="feature-branch", returncode=0),
            ]

            with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
                with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                    with patch("src.auto_coder.issue_processor.get_commit_log", return_value=""):
                        with patch("src.auto_coder.issue_processor.commit_and_push_changes", return_value="Committed"):
                            # Mock GitHub client
                            github_client = MagicMock()
                            github_client.get_parent_issue_details.return_value = None
                            github_client.get_all_sub_issues.return_value = []

                            # Mock LLM response
                            class DummyLLM:
                                def _run_llm_cli(self, *_args, **_kwargs):
                                    return "Made some changes to fix the issue"

                            with patch("src.auto_coder.issue_processor.get_llm_backend_manager", return_value=DummyLLM()):
                                _apply_issue_actions_directly(
                                    repo_name,
                                    issue_data,
                                    config,
                                    github_client,
                                )

        # Verify keep_label was NOT called (no PR creation for PR items)
        assert len(keep_label_called) == 0, "keep_label should not be called for PR items (head_branch set)"


def test_process_issue_jules_mode_passes_all_arguments_to_render_prompt():
    """
    Verify that _process_issue_jules_mode passes all expected arguments
    to render_prompt.
    """
    from src.auto_coder.issue_processor import _process_issue_jules_mode

    repo_name = "test/repo"
    issue_number = 42
    issue_data = {
        "number": issue_number,
        "title": "Test Issue Title",
        "body": "Test Issue Body",
        "labels": [{"name": "bug"}, {"name": "urgent"}],
        "state": "open",
        "user": {"login": "test-user"},
    }
    config = AutomationConfig()
    github_client = MagicMock()

    # Mock parent issue details
    parent_issue_details = {
        "number": 24,
        "title": "Parent Issue Title",
        "body": "Parent Issue Body",
        "state": "OPEN",
    }
    github_client.get_parent_issue_details.return_value = parent_issue_details
    github_client.get_parent_issue_body.return_value = "Parent Issue Body Content"

    # Mock other necessary calls
    with patch("src.auto_coder.issue_processor.JulesClient"), patch(
        "src.auto_coder.issue_processor.CloudManager"
    ), patch(
        "src.auto_coder.issue_processor.get_commit_log",
        return_value="commit log message",
    ), patch(
        "src.auto_coder.issue_processor.render_prompt"
    ) as mock_render_prompt, patch(
        "src.auto_coder.issue_processor.ensure_parent_issue_open", return_value=True
    ), patch(
        "src.auto_coder.issue_processor.cmd"
    ) as mock_cmd:
        mock_cmd.run_command.return_value = _cmd_result(success=True)  # for branch checks

        _process_issue_jules_mode(repo_name, issue_data, config, github_client)

        # Assert render_prompt was called with all the expected arguments
        mock_render_prompt.assert_called_once()
        call_kwargs = mock_render_prompt.call_args.kwargs

        # Arguments from the issue description
        assert call_kwargs.get("repo_name") == repo_name
        assert call_kwargs.get("issue_labels") == "bug, urgent"
        assert call_kwargs.get("issue_state") == issue_data["state"]
        assert call_kwargs.get("issue_author") == "test-user"
        assert call_kwargs.get("parent_issue_number") == parent_issue_details["number"]
        assert call_kwargs.get("parent_issue_title") == parent_issue_details["title"]
        assert call_kwargs.get("parent_issue_body") == "Parent Issue Body Content"
        assert call_kwargs.get("commit_log") == "commit log message"

        # Also check existing arguments
        assert call_kwargs.get("issue_number") == issue_number
        assert call_kwargs.get("issue_title") == issue_data["title"]
        assert call_kwargs.get("issue_body") == issue_data["body"]
