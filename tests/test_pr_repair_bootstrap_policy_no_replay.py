"""Regression tests for Issue #2006.

Auto-Coder must not replay the shared bootstrap policy components
(`policies.short_objective_authoring`, `policies.objective_requirements_boundary`,
`policies.parent_child_contract_boundary`) when it requests PR repairs from an
*existing* cloud implementation session. That session already received those
components on its initial dispatch; resending them on every follow-up round
wastes context and, per the Issue, is unnecessary replay. Fresh/stateless PR
prompts (local test/CI fixes, the independent adversarial reviewer, a fresh
implementation dispatch) must keep receiving them exactly as before.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.issue_processor import _process_issue_codex_cloud_mode
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.pr_processor import (
    _delegate_cloud_merge_conflict_repair_result,
    _delegate_cloud_review_thread_repair,
    _send_adversarial_validation_feedback_to_cloud_task,
    _send_codex_cloud_error_feedback,
)
from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from auto_coder.prompt_loader import render_prompt
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment

SHORT_OBJECTIVE_MARKER = "ISSUE AUTHORING AND REVIEW-RESPONSE POLICY:"
OBJECTIVE_REQUIREMENTS_MARKER = "OBJECTIVE / REQUIREMENTS CONTRACT POLICY:"
PARENT_CHILD_MARKER = "PARENT/CHILD ISSUE CONTRACT BOUNDARY POLICY:"
ALL_MARKERS = (SHORT_OBJECTIVE_MARKER, OBJECTIVE_REQUIREMENTS_MARKER, PARENT_CHILD_MARKER)


# ---------------------------------------------------------------------------
# AS-002 / AS-005 / AS-007 (unit-level composition oracle)
# ---------------------------------------------------------------------------


def test_existing_pr_repair_prompt_omits_all_bootstrap_policies():
    """REQ-003: the wrapper used by every existing-session repair omits all three."""
    prompt = render_prompt(
        "pr.existing_pr_repair",
        repo_name="owner/repo",
        pr_number=200,
        head_branch="issue-100-fix",
        base_branch="main",
        head_sha="H1",
        details="Fix the adversarial validation findings.",
    )

    for marker in ALL_MARKERS:
        assert marker not in prompt
    # REQ-004: repair-specific same-PR/branch invariants and task data survive.
    assert "pull request #200" in prompt
    assert "issue-100-fix" in prompt
    assert "H1" in prompt
    assert "main" in prompt
    assert "Do not create a new branch." in prompt
    assert "Do not create a new pull request." in prompt
    assert "Do not replace or close the existing pull request." in prompt
    assert "Fix the adversarial validation findings." in prompt


def test_quoted_policy_heading_in_corrective_details_survives_as_data():
    """AS-005: a policy heading quoted in supplied review feedback is task data, not injected boilerplate."""
    target = ExistingPrRepairTarget(
        repo_name="owner/repo",
        pr_number=201,
        head_branch="fix-branch",
        base_branch="main",
        head_sha="H2",
    )
    quoted_details = f"Reviewer note: this PR changes the wording of '{OBJECTIVE_REQUIREMENTS_MARKER}' itself. " "Please keep the change and update the referenced policy text accordingly."

    prompt = build_existing_pr_repair_prompt(target, quoted_details)

    # The quoted heading appears exactly once: as the supplied data, never duplicated
    # by an auto-injected copy of the real policy component.
    assert prompt.count(OBJECTIVE_REQUIREMENTS_MARKER) == 1
    assert quoted_details in prompt
    assert PARENT_CHILD_MARKER not in prompt
    assert SHORT_OBJECTIVE_MARKER not in prompt


@pytest.mark.parametrize(
    "key",
    [
        "pr.action",
        "pr.github_actions_fix",
        "pr.local_test_fix",
        "pr.merge_conflict_resolution",
        "pr.adversarial_validation_fix",
        "pr.adversarial_validation_initial_review",
    ],
)
def test_fresh_stateless_pr_prompts_still_receive_contract_boundary_policies(key):
    """AS-003: fresh/stateless PR correction and review prompts are unaffected by this change."""
    prompt = render_prompt(key)
    assert OBJECTIVE_REQUIREMENTS_MARKER in prompt
    assert PARENT_CHILD_MARKER in prompt


@pytest.mark.parametrize("key", ["issue.action", "jules.issue.action"])
def test_fresh_issue_dispatch_receives_all_three_policies_exactly_once(key):
    """REQ-002: new implementation dispatches inject each configured component once."""
    prompt = render_prompt(
        key,
        repo_name="owner/repo",
        issue_number=1,
        issue_title="Title",
        issue_body="Body",
        issue_labels="",
        issue_state="open",
        issue_author="author",
        commit_log="(none)",
        linked_issues_context="",
    )
    for marker in ALL_MARKERS:
        assert prompt.count(marker) == 1


def test_codex_cloud_initial_dispatch_template_receives_all_three_policies_exactly_once():
    prompt = render_prompt(
        "codex_cloud.initial_issue_implementation",
        repo_name="owner/repo",
        base_branch="main",
        issue_number=1,
        issue_url="https://github.com/owner/repo/issues/1",
        issue_title="Title",
        issue_body="Body",
        issue_labels="",
        issue_state="open",
        issue_author="author",
        issue_attempt=1,
        backend_name="codex",
        linked_issues_context="",
        commit_log="(none)",
    )
    for marker in ALL_MARKERS:
        assert prompt.count(marker) == 1


# ---------------------------------------------------------------------------
# AS-001 / AS-002: production-path join across every repair origin
# ---------------------------------------------------------------------------


def _codex_issue(number: int = 1865) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "title": "Fix parser crash",
        "body": ("## Objective\nFix the parser crash on empty input.\n\n" "## Requirements\nREQ-001: The parser must not raise on empty input."),
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"login": "reporter"},
    }


def test_codex_cloud_initial_dispatch_then_every_repair_origin_omits_replayed_policies(tmp_path):
    """AS-001 + AS-002: drive the real Issue dispatch, then the real repair path for
    every supported existing-session repair origin (review, conflict, CI, adversarial),
    using the production association (the Codex task URL embedded in the PR body).
    """
    backend_name = "codex-cloud-main"
    llm_config = LLMBackendConfiguration()
    llm_config.backends[backend_name] = BackendConfig(
        name=backend_name,
        backend_type="codex-cloud",
        environment_id="env-production",
        attempts=3,
    )
    config = AutomationConfig()
    issue = _codex_issue()

    task_id = "task_e_bootstrap9001"
    with (
        patch("auto_coder.codex_cloud_client.get_llm_config", return_value=llm_config),
        patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True),
        patch("auto_coder.issue_processor.get_current_attempt", return_value=1),
        patch("auto_coder.issue_processor.get_commit_log", return_value="commit context"),
        patch("auto_coder.issue_processor.CloudManager") as cloud_manager_type,
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as run_command,
    ):
        cloud_manager_type.return_value.ensure_binding.return_value = True
        run_command.return_value = MagicMock(
            returncode=0,
            stdout=f"https://chatgpt.com/codex/tasks/{task_id}",
            stderr="",
        )
        _process_issue_codex_cloud_mode("owner/repo", issue, config, MagicMock(), backend_name=backend_name)

    # The real transport boundary for the initial dispatch: the CLI argv's final
    # element is the rendered prompt. It must carry each policy exactly once.
    initial_prompt = run_command.call_args.args[0][-1]
    for marker in ALL_MARKERS:
        assert initial_prompt.count(marker) == 1

    # Record the task/session association the dispatch above actually produced
    # through the real production persistence class (CloudManager.ensure_binding
    # is the same call `_process_issue_codex_cloud_mode` itself makes), so the
    # repair path below resolves it, rather than a hand-substituted association.
    with patch("auto_coder.cloud_manager.Path.home", return_value=tmp_path):
        CloudManager("owner/repo").ensure_binding(issue["number"], CloudTaskBinding("codex-cloud", task_id, backend_name))

    # This is exactly how production records/recovers the association for a
    # Codex-created PR: the task URL from the dispatch above, embedded in the
    # PR body (see pr_processor._resolve_codex_cloud_task_id).
    pr_data = {
        "number": 5000,
        "body": (f"Fixes #{issue['number']}\n\n" "https://chatgpt.com/codex/tasks/task_e_bootstrap9001"),
        "head": {"ref": "codex/issue-1865", "sha": "head-1"},
        "base": {"ref": "main"},
    }
    github_client = MagicMock()
    github_client.get_pr_comments.return_value = []
    github_client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(
        head_ref="codex/issue-1865",
        head_sha="head-1",
        base_ref="main",
    )

    # --- Review-thread repair -------------------------------------------------
    thread = ReviewThread(
        id="PRRT_1",
        is_resolved=False,
        comments=[ReviewThreadComment(database_id=1, body="Please handle empty input", author_login="reviewer")],
    )
    with (
        patch("auto_coder.cloud_manager.Path.home", return_value=tmp_path),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "review.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", pr_data, github_client, (thread,))
    review_prompt = send_followup.call_args.args[1]
    for marker in ALL_MARKERS:
        assert marker not in review_prompt
    assert "Please handle empty input" in review_prompt
    assert "codex/issue-1865" in review_prompt
    assert "Do not create a new pull request." in review_prompt

    # --- Merge-conflict repair -------------------------------------------------
    with (
        patch("auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "conflict.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_merge_conflict_repair_result("owner/repo", pr_data, github_client)
    conflict_prompt = send_followup.call_args.args[1]
    for marker in ALL_MARKERS:
        assert marker not in conflict_prompt
    assert "base branch `main`" in conflict_prompt

    # --- CI/test failure continuation ------------------------------------------
    with patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused", return_value=True) as continuation:
        _send_codex_cloud_error_feedback(
            "owner/repo",
            pr_data,
            [{"name": "CI", "conclusion": "failure"}],
            config,
            github_client,
        )
    ci_prompt = continuation.call_args.kwargs["prompt"]
    for marker in ALL_MARKERS:
        assert marker not in ci_prompt
    assert "codex/issue-1865" in ci_prompt

    # --- Adversarial-validation corrective feedback -----------------------------
    finding = "### Auto-Coder adversarial finding\n\nConcrete counterexample about empty input"
    review_client = MagicMock()
    review_client.get_pr_comments.return_value = []
    review_client.get_pr_review_threads_strict.return_value = [ReviewThread(id="PRRT_adv", comments=[ReviewThreadComment(database_id=2, body=finding)])]
    with (
        patch("auto_coder.cloud_manager.Path.home", return_value=tmp_path),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "adversarial.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _send_adversarial_validation_feedback_to_cloud_task("owner/repo", pr_data, "head-1", finding, review_client, [finding])
    adversarial_prompt = send_followup.call_args.args[1]
    for marker in ALL_MARKERS:
        assert marker not in adversarial_prompt
    assert "Concrete counterexample about empty input" in adversarial_prompt


def test_second_repair_round_on_a_new_head_still_omits_replayed_policies(tmp_path):
    """AS-002: more than one successive repair round, each with a distinct PR head,
    keeps omitting the bootstrap policies while preserving its own corrective payload.
    """
    pr_data_round_1 = {
        "number": 5001,
        "body": "https://chatgpt.com/codex/tasks/task_e_round9002",
        "head": {"ref": "codex/issue-2", "sha": "head-round-1"},
        "base": {"ref": "main"},
    }
    pr_data_round_2 = dict(pr_data_round_1, head={"ref": "codex/issue-2", "sha": "head-round-2"})
    github_client = MagicMock()
    github_client.get_pr_comments.return_value = []

    thread_1 = ReviewThread(id="PRRT_r1", comments=[ReviewThreadComment(database_id=10, body="Round 1 finding", author_login="reviewer")])
    thread_2 = ReviewThread(id="PRRT_r2", comments=[ReviewThreadComment(database_id=11, body="Round 2 finding", author_login="reviewer")])

    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("codex-cloud", "task_e_round9002", None)),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "review.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", pr_data_round_1, github_client, (thread_1,))
        _delegate_cloud_review_thread_repair("owner/repo", pr_data_round_2, github_client, (thread_1, thread_2))

    assert send_followup.call_count == 2
    first_prompt, second_prompt = (call.args[1] for call in send_followup.call_args_list)
    for marker in ALL_MARKERS:
        assert marker not in first_prompt
        assert marker not in second_prompt
    assert "Round 1 finding" in first_prompt
    assert "Round 2 finding" in second_prompt
    assert "Round 1 finding" not in second_prompt


# ---------------------------------------------------------------------------
# AS-006: omission never changes a supplied controller decision
# ---------------------------------------------------------------------------


def test_unresolved_origin_still_refuses_delivery_without_sending_anything():
    """Shortening the prompt must never manufacture a task association or a send."""
    pr_data = {"number": 5002, "body": "No linked task here", "head": {"ref": "x", "sha": "H"}, "base": {"ref": "main"}}

    with patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as send_followup:
        result = _delegate_cloud_review_thread_repair(
            "owner/repo",
            pr_data,
            MagicMock(),
            (ReviewThread(id="t", comments=[ReviewThreadComment(database_id=1, body="finding")]),),
        )

    send_followup.assert_not_called()
    assert result.delivered is False
    assert "no provider-owned cloud task association was found" in result[0]


def test_rejected_followup_delivery_remains_retryable_not_terminal(tmp_path):
    """A supplied not-delivered/retry-permitted transport outcome keeps that meaning."""
    pr_data = {
        "number": 5003,
        "body": "https://chatgpt.com/codex/tasks/task_e_retry9003",
        "head": {"ref": "codex/issue-3", "sha": "head-1"},
        "base": {"ref": "main"},
    }
    thread = ReviewThread(id="PRRT_x", comments=[ReviewThreadComment(database_id=1, body="finding")])

    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("codex-cloud", "task_e_retry9003", None)),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "review.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=False) as send_followup,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", pr_data, MagicMock(get_pr_comments=lambda *_: []), (thread,))

    send_followup.assert_called_once()
    assert result.delivered is False
    assert "rejected follow-up delivery" in result[0]
