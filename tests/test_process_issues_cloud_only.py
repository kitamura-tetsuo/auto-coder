"""Regression coverage for cloud dispatch in process-issues --only."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from auto_coder.cli_commands_main import process_issues


def test_process_issues_only_passes_configured_cloud_mode():
    repo_name = "owner/repo"
    llm_config = MagicMock()
    llm_config.get_active_backends.return_value = ["codex-cloud-spark"]
    llm_config.backend_order = ["codex-cloud-spark"]
    llm_config.default_backend = "codex-cloud-spark"

    backend_manager = MagicMock()
    backend_manager._default_backend = "codex-cloud-spark"
    backend_manager._clients = {"codex-cloud-spark": MagicMock()}
    backend_manager._factories = {"codex-cloud-spark": MagicMock()}
    backend_manager._all_backends = ["codex-cloud-spark"]

    message_manager = MagicMock()
    message_manager._default_backend = "qwen"
    message_manager._all_backends = ["qwen"]

    engine = MagicMock()
    engine.process_single.return_value = {
        "repository": repo_name,
        "issues_processed": [{"actions_taken": ["Started Codex Cloud task"]}],
        "prs_processed": [],
        "errors": [],
    }

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value=repo_name),
        patch("auto_coder.cli_commands_main.get_llm_config", return_value=llm_config),
        patch("auto_coder.cli_commands_main.is_jules_mode_enabled", return_value=True) as cloud_mode,
        patch("auto_coder.cli_commands_main.build_models_map", return_value={}),
        patch("auto_coder.cli_commands_main.check_backend_prerequisites"),
        patch("auto_coder.cli_commands_main.ensure_test_script_or_fail"),
        patch("auto_coder.cli_commands_main.setup_progress_footer_logging"),
        patch("auto_coder.cli_commands_main.start_health_monitoring"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=MagicMock()),
        patch("auto_coder.cli_commands_main.build_backend_manager_from_config", return_value=backend_manager),
        patch("auto_coder.cli_commands_main.build_message_backend_manager", return_value=message_manager),
        patch("auto_coder.backend_manager.LLMBackendManager.get_llm_instance"),
        patch("auto_coder.cli_commands_main.AutomationEngine", return_value=engine),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
    ):
        result = CliRunner().invoke(
            process_issues,
            [
                "--repo",
                repo_name,
                "--github-token",
                "token",
                "--only",
                f"https://github.com/{repo_name}/issues/1591",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    cloud_mode.assert_called_once_with(repo_name=repo_name)
    engine.process_single.assert_called_once_with(repo_name, "issue", 1591, jules_mode=True)


@pytest.mark.parametrize(
    ("processing_result", "expected_status"),
    [
        (
            {
                "repository": "owner/repo",
                "issues_processed": [],
                "prs_processed": [{"actions_taken": ["Error budget information only"], "outcome": "success"}],
                "errors": [],
            },
            "Success",
        ),
        (
            {
                "repository": "owner/repo",
                "issues_processed": [],
                "prs_processed": [
                    {
                        "actions_taken": ["Failed to verify remote head SHA: GitHub API rate limited"],
                        "outcome": "failed",
                    }
                ],
                "errors": ["Error processing pr #5266: GitHub API rate limited"],
            },
            "Completed with errors",
        ),
    ],
)
def test_process_issues_only_completion_status_uses_structured_errors(processing_result, expected_status):
    """Completion status is independent of free-form action wording."""
    repo_name = "owner/repo"
    llm_config = MagicMock()
    llm_config.get_active_backends.return_value = ["codex"]
    llm_config.backend_order = ["codex"]
    llm_config.default_backend = "codex"
    backend_manager = MagicMock(_default_backend="codex", _clients={"codex": MagicMock()}, _factories={"codex": MagicMock()}, _all_backends=["codex"])
    message_manager = MagicMock(_default_backend="qwen", _all_backends=["qwen"])
    engine = MagicMock()
    engine.process_single.return_value = processing_result

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value=repo_name),
        patch("auto_coder.cli_commands_main.get_llm_config", return_value=llm_config),
        patch("auto_coder.cli_commands_main.is_jules_mode_enabled", return_value=False),
        patch("auto_coder.cli_commands_main.build_models_map", return_value={}),
        patch("auto_coder.cli_commands_main.check_backend_prerequisites"),
        patch("auto_coder.cli_commands_main.ensure_test_script_or_fail"),
        patch("auto_coder.cli_commands_main.setup_progress_footer_logging"),
        patch("auto_coder.cli_commands_main.start_health_monitoring"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=MagicMock()),
        patch("auto_coder.cli_commands_main.build_backend_manager_from_config", return_value=backend_manager),
        patch("auto_coder.cli_commands_main.build_message_backend_manager", return_value=message_manager),
        patch("auto_coder.backend_manager.LLMBackendManager.get_llm_instance"),
        patch("auto_coder.cli_commands_main.AutomationEngine", return_value=engine),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.print_completion_message") as completion,
    ):
        result = CliRunner().invoke(
            process_issues,
            ["--repo", repo_name, "--github-token", "token", "--only", f"https://github.com/{repo_name}/pull/5266"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert completion.call_args.args[0] == "Processing Complete"
    assert completion.call_args.args[1]["Status"] == expected_status
