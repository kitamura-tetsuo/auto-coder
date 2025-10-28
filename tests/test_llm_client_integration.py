"""Test that LLM clients are properly called in issue and PR processing."""

from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly
from src.auto_coder.pr_processor import _apply_pr_actions_directly


def test_apply_issue_actions_directly_calls_llm_client():
    """Test that _apply_issue_actions_directly calls LLM client."""
    # Setup
    mock_llm_client = Mock()
    mock_llm_client._run_llm_cli = Mock(return_value="Issue analyzed and fixed")

    mock_github_client = Mock()
    mock_github_client.get_parent_issue.return_value = None  # No parent issue

    repo_name = "test/repo"
    issue_data = {
        "number": 123,
        "title": "Test Issue",
        "body": "This is a test issue",
        "labels": ["bug"],
        "state": "open",
        "author": "testuser",
    }
    config = AutomationConfig()
    dry_run = False

    # Execute
    with patch("src.auto_coder.issue_processor.render_prompt") as mock_render:
        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            with patch("src.auto_coder.issue_processor.ensure_pushed") as mock_ensure_pushed:
                mock_render.return_value = "Test prompt for issue"

                # Mock git commands
                def cmd_side_effect(cmd_list, check_success=True):
                    result = Mock()
                    result.success = True
                    result.stdout = ""
                    result.stderr = ""

                    # Work branch does not exist yet
                    if cmd_list == ["git", "rev-parse", "--verify", "issue-123"]:
                        result.success = False
                        result.stderr = "fatal: Needed a single revision"

                    return result

                mock_cmd.run_command.side_effect = cmd_side_effect
                mock_ensure_pushed.return_value = Mock(success=True, stdout="No unpushed commits")

                actions = _apply_issue_actions_directly(
                    repo_name, issue_data, config, dry_run, mock_github_client, mock_llm_client
                )

    # Verify
    mock_llm_client._run_llm_cli.assert_called_once_with("Test prompt for issue")
    assert len(actions) > 0
    assert any("Issue analyzed" in action for action in actions)
    # ブランチ作成または切り替えのメッセージを確認
    assert any("work branch" in action for action in actions)


def test_apply_issue_actions_directly_switches_to_pr_branch():
    """Test that _apply_issue_actions_directly switches to PR branch when head_branch is present."""
    # Setup
    mock_llm_client = Mock()
    mock_llm_client._run_llm_cli = Mock(return_value="Issue analyzed and fixed")

    mock_github_client = Mock()

    repo_name = "test/repo"
    issue_data = {
        "number": 123,
        "title": "Test Issue",
        "body": "This is a test issue",
        "labels": ["bug"],
        "state": "open",
        "author": "testuser",
        "head_branch": "feature/test-branch",  # PRの場合はhead_branchが含まれる
    }
    config = AutomationConfig()
    dry_run = False

    # Execute
    with patch("src.auto_coder.issue_processor.render_prompt") as mock_render:
        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            with patch("src.auto_coder.issue_processor.ensure_pushed") as mock_ensure_pushed:
                mock_render.return_value = "Test prompt for issue"
                mock_result = Mock()
                mock_result.success = True
                mock_result.stdout = ""
                mock_cmd.run_command.return_value = mock_result
                mock_ensure_pushed.return_value = Mock(success=True, stdout="No unpushed commits")

                actions = _apply_issue_actions_directly(
                    repo_name, issue_data, config, dry_run, mock_github_client, mock_llm_client
                )

    # Verify
    mock_llm_client._run_llm_cli.assert_called_once_with("Test prompt for issue")
    # PRブランチに切り替えが最初に呼ばれたことを確認
    assert mock_cmd.run_command.call_count >= 1
    first_call_args = mock_cmd.run_command.call_args_list[0][0][0]
    assert first_call_args[0] == "git"
    assert first_call_args[1] == "checkout"
    assert first_call_args[2] == "feature/test-branch"  # PRのhead_branchに切り替え

    assert len(actions) > 0
    assert any("Issue analyzed" in action for action in actions)
    assert any("Switched to branch" in action for action in actions)


def test_apply_issue_actions_directly_fails_on_branch_switch_error():
    """Test that _apply_issue_actions_directly terminates when branch switch fails."""
    # Setup
    mock_llm_client = Mock()
    mock_llm_client._run_llm_cli = Mock(return_value="Issue analyzed and fixed")

    mock_github_client = Mock()
    mock_github_client.get_parent_issue.return_value = None  # No parent issue

    repo_name = "test/repo"
    issue_data = {
        "number": 123,
        "title": "Test Issue",
        "body": "This is a test issue",
        "labels": ["bug"],
        "state": "open",
        "author": "testuser",
    }
    config = AutomationConfig()
    dry_run = False

    # Execute
    with patch("src.auto_coder.issue_processor.render_prompt") as mock_render:
        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            with patch("src.auto_coder.issue_processor.ensure_pushed") as mock_ensure_pushed:
                mock_render.return_value = "Test prompt for issue"
                mock_result = Mock()
                mock_result.success = False
                mock_result.stderr = "fatal: branch not found"
                mock_cmd.run_command.return_value = mock_result
                mock_ensure_pushed.return_value = Mock(success=True, stdout="No unpushed commits")

                actions = _apply_issue_actions_directly(
                    repo_name, issue_data, config, dry_run, mock_github_client, mock_llm_client
                )

    # Verify
    # LLMは呼ばれないはず（ブランチ切り替えで失敗して終了）
    mock_llm_client._run_llm_cli.assert_not_called()
    # ブランチ切り替えのエラーメッセージが含まれているはず
    assert len(actions) == 1
    assert "Failed to" in actions[0]
    assert "fatal: branch not found" in actions[0]


def test_apply_pr_actions_directly_calls_llm_client():
    """Test that _apply_pr_actions_directly calls LLM client."""
    # Setup
    mock_llm_client = Mock()
    mock_llm_client._run_llm_cli = Mock(return_value="ACTION_SUMMARY: Fixed PR issues")

    repo_name = "test/repo"
    pr_data = {
        "number": 456,
        "title": "Test PR",
        "body": "This is a test PR",
        "labels": ["enhancement"],
        "state": "open",
        "author": "testuser",
    }
    config = AutomationConfig()
    dry_run = False

    # Execute
    with patch("src.auto_coder.pr_processor._get_pr_diff") as mock_diff:
        with patch(
            "src.auto_coder.pr_processor._create_pr_analysis_prompt"
        ) as mock_prompt:
            mock_diff.return_value = "diff content"
            mock_prompt.return_value = "Test prompt for PR"
            actions = _apply_pr_actions_directly(
                repo_name, pr_data, config, dry_run, mock_llm_client
            )

    # Verify
    mock_llm_client._run_llm_cli.assert_called_once_with("Test prompt for PR")
    assert len(actions) > 0
    assert any("ACTION_SUMMARY" in action for action in actions)
