"""Regression coverage for cloud dispatch in process-issues --only."""

from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig, Candidate
from auto_coder.automation_engine import AutomationEngine
from auto_coder.cli_commands_main import process_issues


@pytest.mark.parametrize("retry", [False, True])
def test_process_issues_only_passes_configured_cloud_mode(retry):
    parent = click.Context(click.Command("auto-coder"))
    parent.params["force"] = retry
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
                *(["--retry"] if retry else []),
                "--repo",
                repo_name,
                "--github-token",
                "token",
                "--only",
                f"https://github.com/{repo_name}/issues/1591",
            ],
            catch_exceptions=False,
            parent=parent,
        )

    assert result.exit_code == 0
    cloud_mode.assert_called_once_with(repo_name=repo_name)
    engine.process_single.assert_called_once_with(
        repo_name,
        "issue",
        1591,
        jules_mode=True,
        explicit_only=True,
        force=retry,
        **({"retry": True} if retry else {}),
    )


@pytest.mark.parametrize(
    ("processing_result", "expected_status"),
    [
        (
            {
                "target_number": 5266,
                "target_type": None,
                "target_outcome": "deferred",
                "target_reason": "GitHub request deferred before sending: rate_limit_cooldown; retry_at=1789286881.7999094 (Unix seconds)",
                "errors": ["GitHub request deferred before sending: rate_limit_cooldown; retry_at=1789286881.7999094 (Unix seconds)"],
            },
            "Deferred",
        ),
        *[
            (
                {
                    "target_number": number,
                    "target_type": None,
                    "target_outcome": outcome,
                    "errors": errors,
                },
                "Failed",
            )
            for number, outcome, errors in [
                (5266, "failed", ["GitHub request deferred before sending: governor_state_unavailable"]),
                (9999, "failed", ["Lookup failed"]),
                (9999, "deferred", ["Lookup failed"]),
                (5266, "deferred", []),
                (5266, "success", []),
                (5266, "failed", []),
            ]
        ],
        (
            {
                "repository": "owner/repo",
                "target_number": 5266,
                "target_type": "pr",
                "target_outcome": "success",
                "target_actions": ["Error budget information only"],
                "issues_processed": [],
                "prs_processed": [{"actions_taken": ["Error budget information only"], "outcome": "success"}],
                "errors": [],
            },
            "Success",
        ),
        (
            {
                "repository": "owner/repo",
                "target_number": 5266,
                "target_type": "pr",
                "target_outcome": "failed",
                "target_actions": ["Failed to verify remote head SHA: GitHub API rate limited"],
                "issues_processed": [],
                "prs_processed": [
                    {
                        "actions_taken": ["Failed to verify remote head SHA: GitHub API rate limited"],
                        "outcome": "failed",
                    }
                ],
                "errors": ["Error processing pr #5266: GitHub API rate limited"],
            },
            "Failed",
        ),
        (
            {
                "repository": "owner/repo",
                "target_number": 5266,
                "target_type": "pr",
                "target_outcome": "deferred",
                "target_actions": ["Waiting for required checks"],
                "target_reason": "Required checks are pending",
                "issues_processed": [],
                "prs_processed": [],
                "errors": [],
            },
            "Deferred",
        ),
        (
            {
                "repository": "owner/repo",
                "target_number": 5266,
                "target_type": "pr",
                "target_outcome": "skipped",
                "target_actions": ["Author is not allowed"],
                "issues_processed": [],
                "prs_processed": [],
                "errors": [],
            },
            "Skipped",
        ),
        (
            {
                "repository": "owner/repo",
                "target_number": 5266,
                "target_type": "pr",
                "target_outcome": "blocked",
                "target_actions": ["Specification must change"],
                "issues_processed": [],
                "prs_processed": [],
                "errors": [],
            },
            "Blocked",
        ),
        (
            {
                "repository": "owner/repo",
                "target_number": 9999,
                "target_type": "pr",
                "target_outcome": "success",
                "issues_processed": [],
                "prs_processed": [],
                "errors": [],
            },
            "Failed",
        ),
    ],
)
def test_process_issues_only_completion_status_uses_target_outcome(processing_result, expected_status):
    """Completion status fails closed around the explicit target contract."""
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
    if processing_result.get("target_type") is None:
        errors = completion.call_args.args[1]["Errors"]
        if processing_result["target_number"] != 5266:
            assert errors == ["Lookup failed", "Explicit result target mismatch: requested #5266, received #9999"]
        elif processing_result["errors"]:
            assert errors == processing_result["errors"]
        else:
            assert errors == ["Explicit result did not provide an authoritative target type for #5266"]
    if expected_status != "Success":
        assert "Processed single" not in result.output


def test_process_issues_only_preserves_production_deferred_issue_outcome():
    """A normal candidate gate survives the engine-to-CLI result boundary."""
    repo_name = "owner/repo"
    issue_number = 1807
    issue = {
        "number": issue_number,
        "title": "Child implementation",
        "body": "",
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github = MagicMock()
    github.get_direct_sub_issues_strict.return_value = []
    engine = AutomationEngine(github, config=AutomationConfig())
    engine._check_and_handle_closed_branch = MagicMock(return_value=True)
    engine._create_candidate_from_single = MagicMock(return_value=Candidate(type="issue", data=dict(issue), priority=1, issue_number=issue_number))
    engine._preflight_explicit_issue_relationships = MagicMock(return_value=dict(issue))
    engine._validate_submitted_parent_generation_for_child = MagicMock()
    engine._is_issue_author_allowed = MagicMock(return_value=True)
    engine._get_authoritative_parent_number = MagicMock(return_value=None)
    engine._defer_initial_issue_stabilization = MagicMock(return_value=True)

    llm_config = MagicMock()
    llm_config.get_active_backends.return_value = ["codex"]
    llm_config.backend_order = ["codex"]
    llm_config.default_backend = "codex"
    backend_manager = MagicMock(
        _default_backend="codex",
        _clients={"codex": MagicMock()},
        _factories={"codex": MagicMock()},
        _all_backends=["codex"],
    )
    message_manager = MagicMock(_default_backend="qwen", _all_backends=["qwen"])

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value=repo_name),
        patch("auto_coder.cli_commands_main.get_llm_config", return_value=llm_config),
        patch("auto_coder.cli_commands_main.is_jules_mode_enabled", return_value=False),
        patch("auto_coder.cli_commands_main.build_models_map", return_value={}),
        patch("auto_coder.cli_commands_main.check_backend_prerequisites"),
        patch("auto_coder.cli_commands_main.ensure_test_script_or_fail"),
        patch("auto_coder.cli_commands_main.setup_progress_footer_logging"),
        patch("auto_coder.cli_commands_main.start_health_monitoring"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.cli_commands_main.build_backend_manager_from_config", return_value=backend_manager),
        patch("auto_coder.cli_commands_main.build_message_backend_manager", return_value=message_manager),
        patch("auto_coder.backend_manager.LLMBackendManager.get_llm_instance"),
        patch("auto_coder.cli_commands_main.AutomationEngine", return_value=engine),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.print_completion_message") as completion,
    ):
        invocation = CliRunner().invoke(
            process_issues,
            ["--repo", repo_name, "--github-token", "token", "--only", str(issue_number)],
            catch_exceptions=False,
        )

    assert invocation.exit_code == 0
    summary = completion.call_args.args[1]
    assert summary["Status"] == "Deferred"
    assert summary["Target"].startswith(f"issue #{issue_number}")
    assert summary["Actions Taken"] == ["Deferred - readiness submission is in its initial stabilization window"]
    assert "Processed single" not in invocation.output


@pytest.mark.parametrize("only,force", [(False, False), (True, False), (False, True)])
def test_retry_requires_both_only_and_force_before_setup(only, force):
    parent = click.Context(click.Command("auto-coder"))
    parent.params["force"] = force
    with patch("auto_coder.cli_commands_main.get_repo_or_detect") as detect:
        result = CliRunner().invoke(process_issues, ["--retry", *(["--only", "2014"] if only else [])], parent=parent)
    assert result.exit_code == 2
    assert "--retry requires --only and --force" in result.output
    detect.assert_not_called()
