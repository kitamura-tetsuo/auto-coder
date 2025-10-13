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
        mock_render.return_value = "Test prompt for issue"
        actions = _apply_issue_actions_directly(
            repo_name, issue_data, config, dry_run, mock_llm_client
        )

    # Verify
    mock_llm_client._run_llm_cli.assert_called_once_with("Test prompt for issue")
    assert len(actions) > 0
    assert any("Issue analyzed" in action for action in actions)





def test_apply_pr_actions_directly_calls_llm_client():
    """Test that _apply_pr_actions_directly calls LLM client."""
    # Setup
    mock_llm_client = Mock()
    mock_llm_client._run_llm_cli = Mock(
        return_value="ACTION_SUMMARY: Fixed PR issues"
    )

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
        with patch("src.auto_coder.pr_processor._create_pr_analysis_prompt") as mock_prompt:
            mock_diff.return_value = "diff content"
            mock_prompt.return_value = "Test prompt for PR"
            actions = _apply_pr_actions_directly(
                repo_name, pr_data, config, dry_run, mock_llm_client
            )

    # Verify
    mock_llm_client._run_llm_cli.assert_called_once_with("Test prompt for PR")
    assert len(actions) > 0
    assert any("ACTION_SUMMARY" in action for action in actions)




