"""Production-path coverage for the initial Codex Cloud publication contract."""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.issue_processor import (
    _process_issue_cloud_backend,
    _process_issue_high_score_cloud,
)
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER


@pytest.mark.parametrize("dispatcher_name", ["ordinary", "high-score"])
def test_initial_dispatch_renders_and_transports_complete_provider_prompt(tmp_path, monkeypatch, dispatcher_name):
    """The production dispatcher and argv adapter preserve exact hostile Issue data."""
    sentinel = tmp_path / "must-not-exist"
    issue_body = "## Objective\n背景を維持する。\n\n## Requirements\n" "REQ-001: preserve `code`, quotes \"and 'quotes'\", and literal $HOME.\n" f"REQ-LAST: never execute $(touch {sentinel})."
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    backend_name = "codex-cloud-repository-special"
    llm_config = LLMBackendConfiguration()
    if dispatcher_name == "ordinary":
        llm_config.backend_cloud_order = [backend_name]
        dispatcher = _process_issue_cloud_backend
    else:
        llm_config.backend_with_high_score_cloud_order = [backend_name]
        dispatcher = _process_issue_high_score_cloud
    llm_config.backends[backend_name] = BackendConfig(
        name=backend_name,
        backend_type="codex-cloud",
        environment_id="env-production-test",
        attempts=3,
    )
    config = AutomationConfig()
    config.MAIN_BRANCH = "release/次"
    issue = {
        "number": 1865,
        "html_url": "https://github.com/owner/repo/issues/1865",
        "title": "Full title — no truncation",
        "body": issue_body,
        "labels": [{"name": "implementation-ready"}, {"name": "@auto-coder"}],
        "state": "open",
        "user": {"login": "reporter"},
        "parent_issue_number": 1861,
        "parent_issue_title": "Background parent",
        "parent_issue_body": "Parent capability is context, not child scope.",
        "linked_issues_context": "Linked #1864 is supplied context only.",
    }

    with (
        patch(
            "auto_coder.llm_backend_config.get_llm_config",
            return_value=llm_config,
        ),
        patch("auto_coder.codex_cloud_client.get_llm_config", return_value=llm_config),
        patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True),
        patch("auto_coder.issue_processor.get_current_attempt", return_value=4),
        patch("auto_coder.issue_processor.get_commit_log", return_value="commit context"),
        patch(
            "auto_coder.quota_selector.rank_high_score_backends_by_quota",
            return_value=[backend_name],
        ),
        patch("auto_coder.issue_processor.CloudManager") as cloud_manager_type,
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as run_command,
    ):
        cloud_manager_type.return_value.ensure_binding.return_value = True
        run_command.return_value = MagicMock(
            returncode=0,
            stdout="https://chatgpt.com/codex/tasks/task_e_6a26c19ac8a88326af83ebfb44b89fe2",
            stderr="",
        )
        actions = dispatcher("owner/repo", issue, config, MagicMock())

    argv = run_command.call_args.args[0]
    assert argv[:-1] == [
        "codex",
        "cloud",
        "exec",
        "--env",
        "env-production-test",
        "--attempts",
        "3",
        "--branch",
        "release/次",
    ]
    prompt = argv[-1]
    assert issue_body in prompt
    assert "REQ-LAST" in prompt
    assert "https://github.com/owner/repo/issues/1865" in prompt
    assert "Issue attempt 4" in prompt
    assert backend_name in prompt
    assert "Actual base branch: release/次" in prompt
    assert "Parent capability is context, not child scope." in prompt
    assert "Linked #1864 is supplied context only." in prompt
    assert "complete and untruncated" in prompt
    assert "Issue Description:" not in prompt
    assert "@auto-coder" not in prompt
    assert "$issue_" not in prompt
    assert REVIEW_ADDRESSED_MARKER in prompt
    assert not sentinel.exists()
    assert actions == ["Started Codex Cloud task 'task_e_6a26c19ac8a88326af83ebfb44b89fe2' for issue #1865"]


def test_initial_contract_is_not_added_to_existing_pr_repair_prompt():
    """Initial publication and existing-PR repair retain opposite ownership."""
    from auto_coder.prompt_loader import get_prompt_template

    initial = get_prompt_template("codex_cloud.initial_issue_implementation")
    repair = get_prompt_template("pr.existing_pr_repair")

    assert "publish a GitHub pull request" in initial
    assert "shared/transient remote branch named `work`" in initial
    assert "real, verified GitHub PR URL" in initial
    assert "Do not create a new branch." in repair
    assert "Do not create a new pull request." in repair
    assert "real, verified GitHub PR URL" not in repair
