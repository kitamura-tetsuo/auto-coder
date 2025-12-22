from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from src.auto_coder.cli_commands_main import process_issues


class StopLoop(Exception):
    pass


@pytest.mark.parametrize(
    "open_issues_count, open_prs_count, processed_issues, processed_prs, expected_sleep_type",
    [
        (0, 0, 0, 0, "empty"),  # No open items, nothing processed -> Long sleep
        (1, 0, 0, 0, "short"),  # Open issues exist, nothing processed -> Short sleep
        (0, 1, 0, 0, "short"),  # Open PRs exist, nothing processed -> Short sleep
        (1, 1, 0, 0, "short"),  # Both exist, nothing processed -> Short sleep
        (0, 0, 1, 0, "empty"),  # Processed issues, but now ZERO open -> Long sleep (Logic changed to depend ONLY on OPEN count)
    ],
)
@patch("src.auto_coder.cli_commands_main.get_llm_config")
@patch("src.auto_coder.cli_commands_main.GitHubClient")
@patch("src.auto_coder.cli_commands_main.AutomationEngine")
@patch("src.auto_coder.cli_commands_main.sleep_with_countdown")
@patch("src.auto_coder.llm_backend_config.get_process_issues_empty_sleep_time_from_config")
@patch("src.auto_coder.llm_backend_config.get_process_issues_sleep_time_from_config")
@patch("src.auto_coder.cli_commands_main.get_current_branch")
@patch("src.auto_coder.cli_commands_main.setup_progress_footer_logging")  # dependency
@patch("src.auto_coder.cli_commands_main.ensure_test_script_or_fail")  # dependency
@patch("src.auto_coder.cli_commands_main.initialize_graphrag")  # dependency
@patch("src.auto_coder.cli_commands_main.build_backend_manager_from_config")  # dependency
@patch("src.auto_coder.backend_manager.LLMBackendManager")  # dependency
@patch("src.auto_coder.cli_commands_main.check_graphrag_mcp_for_backends")  # dependency
@patch("src.auto_coder.cli_commands_main.build_message_backend_manager")  # dependency
def test_process_issues_sleep_logic(
    mock_msg_mgr,
    mock_check_mcp,
    mock_llm_mgr,
    mock_build_backend,
    mock_init_graphrag,
    mock_ensure_test,
    mock_setup_footer,
    mock_get_branch,
    mock_get_short_sleep,
    mock_get_empty_sleep,
    mock_sleep_countdown,
    mock_engine_cls,
    mock_gh_cls,
    mock_config,
    open_issues_count,
    open_prs_count,
    processed_issues,
    processed_prs,
    expected_sleep_type,
):
    # Prevent early return by forcing main branch
    mock_get_branch.return_value = "main"

    # Setup sleep duration constants
    SHORT_SLEEP = 5
    EMPTY_SLEEP = 60
    mock_get_short_sleep.return_value = SHORT_SLEEP
    mock_get_empty_sleep.return_value = EMPTY_SLEEP

    # Setup mocks
    mock_gh_instance = MagicMock()
    mock_gh_cls.get_instance.return_value = mock_gh_instance

    # Mock open issues/prs return values
    # get_open_issues returns a list. logic checks len() > 0
    mock_gh_instance.get_open_issues.return_value = ["issue"] * open_issues_count
    mock_gh_instance.get_open_pull_requests.return_value = ["pr"] * open_prs_count

    # Mock engine run result
    mock_engine_instance = MagicMock()
    mock_engine_cls.return_value = mock_engine_instance
    mock_engine_instance.run.return_value = {"issues_processed": ["i"] * processed_issues, "prs_processed": ["p"] * processed_prs}

    # Mock config
    mock_config_instance = MagicMock()
    mock_config.return_value = mock_config_instance
    mock_config_instance.get_active_backends.return_value = ["gemini"]
    mock_config_instance.backend_order = []
    mock_config_instance.default_backend = "gemini"
    mock_config_instance.get_backend_config.return_value = MagicMock(api_key="dummy")

    # Mock sleep to raise exception to break loop
    mock_sleep_countdown.side_effect = StopLoop()

    runner = CliRunner()
    result = runner.invoke(process_issues, ["--repo", "owner/repo", "--github-token", "dummy", "--disable-graphrag"])

    # Verify we checked open issues/prs with limit=1
    mock_gh_instance.get_open_issues.assert_called_with("owner/repo", limit=1)
    mock_gh_instance.get_open_pull_requests.assert_called_with("owner/repo", limit=1)

    # Check correct sleep time was used
    if expected_sleep_type == "short":
        mock_sleep_countdown.assert_called_with(SHORT_SLEEP)
    else:
        mock_sleep_countdown.assert_called_with(EMPTY_SLEEP)
