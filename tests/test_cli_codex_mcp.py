from unittest.mock import Mock, patch
from click.testing import CliRunner

from src.auto_coder.cli import process_issues, fix_to_pass_tests_command


@patch("src.auto_coder.cli.check_codex_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.CodexMCPClient")
@patch("src.auto_coder.cli.GitHubClient")
def test_process_issues_backend_codex_mcp_warns_and_closes(
    mock_github_client_class,
    mock_codex_mcp_client_class,
    mock_automation_engine_class,
    mock_check_cli,
):
    mock_github_client_class.return_value = Mock()
    mock_ai_client = Mock()
    mock_codex_mcp_client_class.return_value = mock_ai_client
    mock_automation_engine_class.return_value = Mock()
    mock_check_cli.return_value = None

    runner = CliRunner()
    result = runner.invoke(
        process_issues,
        [
            "--repo",
            "test/repo",
            "--github-token",
            "token",
            "--backend",
            "codex-mcp",
            "--model",
            "ignored-model",
        ],
    )

    assert result.exit_code == 0
    assert "Using backends: codex-mcp (default: codex-mcp)" in result.output
    assert "Warning: --model is ignored when backend=codex or codex-mcp" in result.output
    # ensured the MCP client was used and closed
    mock_codex_mcp_client_class.assert_called_once()
    mock_ai_client.close.assert_called_once()


@patch("src.auto_coder.cli.check_codex_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.CodexMCPClient")
def test_fix_to_pass_tests_backend_codex_mcp_closes(
    mock_codex_mcp_client_class,
    mock_automation_engine_class,
    mock_check_cli,
):
    mock_ai_client = Mock()
    mock_codex_mcp_client_class.return_value = mock_ai_client
    engine = Mock()
    engine.fix_to_pass_tests.return_value = {"success": True, "attempts": 1}
    mock_automation_engine_class.return_value = engine

    runner = CliRunner()
    result = runner.invoke(
        fix_to_pass_tests_command,
        ["--backend", "codex-mcp"],
    )

    assert result.exit_code == 0
    assert "Using backends: codex-mcp (default: codex-mcp)" in result.output
    mock_ai_client.close.assert_called_once()

