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

from auto_coder import prompt_loader
from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.issue_processor import (
    _process_issue_claude_routine_mode,
    _process_issue_codex_cloud_mode,
    _process_issue_jules_mode,
)
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


@pytest.fixture
def cloud_home(tmp_path, monkeypatch):
    """Route CloudManager/CloudRunRepository production persistence at a temp
    HOME so the real disk-backed association write/read path is exercised
    (matching the pattern in test_cloud_conflict_delegation.py) without
    touching the real ~/.auto-coder directory.
    """
    monkeypatch.setattr("auto_coder.cloud_manager.Path.home", lambda: tmp_path)
    monkeypatch.setattr("auto_coder.cloud_run.Path.home", lambda: tmp_path)
    return tmp_path


def _minimal_prompts_yaml(directory, *, short_sentinel: str, boundary_sentinel: str, parent_sentinel: str):
    """Write a self-contained prompts.yaml with distinctive sentinel policy
    bodies that share no text with the real default headings, so a test using
    it can prove omission targets the *effective configured content*, not a
    hard-coded default heading string.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "prompts.yaml"
    path.write_text(
        f"""
header: "Read AGENTS.md."
jules_header: "Read AGENTS.md."

policies:
  short_objective_authoring: |-
    {short_sentinel}
    Distinctive configured body text about objective authoring.
  objective_requirements_boundary: |-
    {boundary_sentinel}
    Distinctive configured body text about the requirements boundary.
  parent_child_contract_boundary: |-
    {parent_sentinel}
    Distinctive configured body text about parent/child contract scope.

issue:
  action: |-
    Issue #$issue_number: $issue_title
    $issue_body

codex_cloud:
  initial_issue_implementation: |-
    Implement issue #$issue_number ($issue_title) in $repo_name on branch $base_branch.
    $issue_body
  review_thread_repair_details: |-
    $actionable_feedback
  ci_review_repair_details: |-
    Investigate and fix the CI failures.

pr:
  existing_pr_repair: |-
    You are repairing pull request #$pr_number in $repo_name.
    Work only on `$head_branch` (base `$base_branch`, sha $head_sha).
    Do not create a new branch. Do not create a new pull request.
    $details
""",
        encoding="utf-8",
    )
    return path


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


def test_codex_cloud_initial_dispatch_then_every_repair_origin_omits_replayed_policies(cloud_home):
    """AS-001 + AS-002: drive the real Issue dispatch, then the real repair path for
    every supported existing-session repair origin (review, conflict, CI, adversarial),
    using the production association (the Codex task URL embedded in the PR body).

    The initial dispatch's `CloudManager.ensure_binding()` call and the repair
    path's `CloudManager.get_binding()`/`CloudRunRepository` lookups are both
    real (only `cloud_home` redirects their storage to a temp HOME and only the
    external `codex` CLI transport is mocked), so this proves the production
    dispatch actually persisted the association the repair path then resolves —
    not a handcrafted substitute for it.
    """
    tmp_path = cloud_home
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
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as run_command,
    ):
        run_command.return_value = MagicMock(
            returncode=0,
            stdout=f"https://chatgpt.com/codex/tasks/{task_id}",
            stderr="",
        )
        dispatch_actions = _process_issue_codex_cloud_mode("owner/repo", issue, config, MagicMock(), backend_name=backend_name)

    # The dispatch itself reports success, which for Codex Cloud only happens
    # after `CloudManager.ensure_binding()` succeeded (see issue_processor.py):
    # a failed/incomplete binding write returns a "tracking is incomplete"
    # message instead. This is the observable proof the real write happened.
    assert dispatch_actions == [f"Started Codex Cloud task '{task_id}' for issue #{issue['number']}"]

    # The real transport boundary for the initial dispatch: the CLI argv's final
    # element is the rendered prompt. It must carry each policy exactly once.
    initial_prompt = run_command.call_args.args[0][-1]
    for marker in ALL_MARKERS:
        assert initial_prompt.count(marker) == 1

    # Read the association back through the same production class the repair
    # path itself uses (CloudManager.get_binding), rather than asserting on
    # private file contents, and confirm it is exactly what the dispatch above
    # produced (not a value this test injected).
    resolved_binding = CloudManager("owner/repo").get_binding(issue["number"])
    assert resolved_binding == CloudTaskBinding("codex-cloud", task_id, backend_name)

    # This is exactly how production records/recovers the association for a
    # Codex-created PR: the task URL from the dispatch above, embedded in the
    # PR body (see pr_processor._resolve_codex_cloud_task_id).
    pr_data = {
        "number": 5000,
        "body": (f"Fixes #{issue['number']}\n\n" f"https://chatgpt.com/codex/tasks/{task_id}"),
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
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "adversarial.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _send_adversarial_validation_feedback_to_cloud_task("owner/repo", pr_data, "head-1", finding, review_client, [finding])
    adversarial_prompt = send_followup.call_args.args[1]
    for marker in ALL_MARKERS:
        assert marker not in adversarial_prompt
    assert "Concrete counterexample about empty input" in adversarial_prompt


def test_jules_initial_dispatch_then_review_repair_omits_replayed_policies(cloud_home):
    """AS-001/AS-002 for the Jules provider: real dispatch persists the session
    via `CloudManager.add_session()`; the repair path reads that same real
    binding back (through the linked-issue-number resolution production PRs
    actually use) and delivers the follow-up through `JulesClient.send_followup`.
    """
    tmp_path = cloud_home
    issue = {"number": 1902, "title": "Fix null pointer", "body": "Body", "labels": [], "state": "open", "user": {"login": "reporter"}}
    session_id = "jules-session-bootstrap"
    github_client = MagicMock()

    with (
        patch("auto_coder.issue_processor.get_commit_log", return_value="commit context"),
        patch("auto_coder.jules_client.JulesClient.start_session", return_value=session_id) as start_session,
    ):
        actions = _process_issue_jules_mode("owner/repo", issue, AutomationConfig(), github_client)

    assert any(f"Started Jules session '{session_id}'" in action for action in actions)
    initial_prompt = start_session.call_args.args[0]
    for marker in ALL_MARKERS:
        assert initial_prompt.count(marker) == 1

    # Read the association back the same way the repair path does.
    resolved_binding = CloudManager("owner/repo").get_binding(issue["number"])
    assert resolved_binding.provider == "jules"
    assert resolved_binding.task_id == session_id

    pr_data = {
        "number": 6000,
        "body": f"Fixes #{issue['number']}\n\nCreated by Jules.",
        "head": {"ref": "jules/issue-1902", "sha": "head-1"},
        "base": {"ref": "main"},
    }
    thread = ReviewThread(id="PRRT_jules", comments=[ReviewThreadComment(database_id=1, body="Please add a null check", author_login="reviewer")])
    github_client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(
        head_ref="jules/issue-1902",
        head_sha="head-1",
        base_ref="main",
    )

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "review.json"),
        patch("auto_coder.jules_client.JulesClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", pr_data, github_client, (thread,))

    send_followup.assert_called_once()
    task_id, review_prompt = send_followup.call_args.args
    assert task_id == session_id
    for marker in ALL_MARKERS:
        assert marker not in review_prompt
    assert "Please add a null check" in review_prompt
    assert "jules/issue-1902" in review_prompt
    assert "Do not create a new pull request." in review_prompt


def test_claude_routine_named_backend_dispatch_then_conflict_repair_omits_replayed_policies(cloud_home):
    """AS-001/AS-002 for Claude Routine with a *named* backend: real dispatch
    persists the session via the production `CloudManager` write inside
    `_process_issue_claude_routine_mode()`; the merge-conflict repair path
    resolves the same real association and delivers through the named
    backend's CLI transport (`claude -p --cloud=<session> ...`).
    """
    tmp_path = cloud_home
    backend_name = "claude-named-backend"
    backend = BackendConfig(name=backend_name, backend_type="claude-routine", url="https://claude-named.example/fire", api_key="token-named")
    llm_config = MagicMock()
    llm_config.get_backend_config.return_value = backend
    github = MagicMock()
    issue = {"number": 1903, "title": "Fix race condition", "body": "Details", "labels": [], "state": "open"}
    pull_request = {
        "number": 6001,
        "body": "Fixes #1903",
        "head": {"ref": "cloud/repair-1903", "sha": "head-1"},
        "base": {"ref": "main", "sha": "base-1"},
    }
    pull_request["user"] = {"login": "claude[bot]"}

    with (
        patch("auto_coder.claude_routine_client.get_llm_config", return_value=llm_config),
        patch("auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine", return_value=("session-named", None)) as fire_routine,
        patch("auto_coder.issue_processor.get_commit_log", return_value="initial"),
    ):
        _process_issue_claude_routine_mode("owner/repo", issue, AutomationConfig(), github, backend_name=backend_name)

    initial_prompt = fire_routine.call_args.args[0]
    for marker in ALL_MARKERS:
        assert initial_prompt.count(marker) == 1

    resolved_binding = CloudManager("owner/repo").get_binding(issue["number"])
    assert resolved_binding == CloudTaskBinding("claude-routine", "session-named", backend_name)

    with (
        patch("auto_coder.claude_routine_client.get_llm_config", return_value=llm_config),
        patch("auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "conflict.json"),
        patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as command,
    ):
        command.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = _delegate_cloud_merge_conflict_repair_result("owner/repo", pull_request, github)

    assert result.delegated is True
    args, kwargs = command.call_args
    assert args[0][:3] == ["claude", "-p", "--cloud=session-named"]
    conflict_prompt = args[0][3]
    for marker in ALL_MARKERS:
        assert marker not in conflict_prompt
    assert "cloud/repair-1903" in conflict_prompt
    assert kwargs["env"]["CLAUDE_CODE_ROUTINE_TOKEN"] == "token-named"


# ---------------------------------------------------------------------------
# AS-005 / AS-007: the oracle must track effective configured content, not
# only the default heading strings (a heading-only-stripping implementation
# must fail these).
# ---------------------------------------------------------------------------


def test_distinctive_configured_policy_content_without_default_headings_is_omitted_correctly(tmp_path):
    """An implementation that only strips the three default heading lines
    (leaving each policy's body text behind) must fail this: the sentinel
    strings below share no text with `ALL_MARKERS`, so the assertions can
    only pass if the *entire effective configured component* is omitted.
    """
    short_sentinel = "SENTINEL-SHORT-OBJECTIVE-9f3a2c"
    boundary_sentinel = "SENTINEL-OBJ-REQ-BOUNDARY-2c71e4"
    parent_sentinel = "SENTINEL-PARENT-CHILD-77e0b1"
    prompts_path = _minimal_prompts_yaml(
        tmp_path,
        short_sentinel=short_sentinel,
        boundary_sentinel=boundary_sentinel,
        parent_sentinel=parent_sentinel,
    )

    fresh_prompt = render_prompt(
        "issue.action",
        path=str(prompts_path),
        issue_number=1,
        issue_title="Sentinel issue",
        issue_body="Sentinel body",
    )
    for sentinel in (short_sentinel, boundary_sentinel, parent_sentinel):
        assert fresh_prompt.count(sentinel) == 1

    repair_prompt = render_prompt(
        "pr.existing_pr_repair",
        path=str(prompts_path),
        repo_name="owner/repo",
        pr_number=1,
        head_branch="b",
        base_branch="main",
        head_sha="H",
        details=("Fix the reported defect. This PR also updates the wording of the " f"'{boundary_sentinel}' policy component, quoted here verbatim: {boundary_sentinel}."),
    )
    # None of the three configured components is auto-injected into the
    # continuation...
    assert short_sentinel not in repair_prompt
    assert parent_sentinel not in repair_prompt
    # ...but the sentinel quoted as task data survives untouched, and exactly
    # as many times as the supplied text actually contains it (twice here),
    # never duplicated by a real injected copy of the component.
    assert repair_prompt.count(boundary_sentinel) == 2


# ---------------------------------------------------------------------------
# AS-004: a restart/config reload changes what a *fresh* context receives,
# but never refreshes or replays into an already-resolved existing session.
# ---------------------------------------------------------------------------


def test_restart_with_new_effective_policy_config_does_not_refresh_existing_session(cloud_home):
    """Dispatch under policy config A, "restart" into config B (a fresh
    `DEFAULT_PROMPTS_PATH` plus a cleared cache, exactly what a process
    restart after a config change looks like from `prompt_loader`'s
    perspective), then repair the *original* session. Neither A nor B leaks
    into the repair continuation, while a brand-new task dispatched after the
    reload receives B. The association is read back through the same
    production `CloudManager` boundary used elsewhere in this file, not a
    process-local prompt history.
    """
    tmp_path = cloud_home
    config_a = _minimal_prompts_yaml(
        tmp_path / "config-a",
        short_sentinel="SENTINEL-A-SHORT-1111",
        boundary_sentinel="SENTINEL-A-BOUNDARY-2222",
        parent_sentinel="SENTINEL-A-PARENT-3333",
    )
    config_b = _minimal_prompts_yaml(
        tmp_path / "config-b",
        short_sentinel="SENTINEL-B-SHORT-4444",
        boundary_sentinel="SENTINEL-B-BOUNDARY-5555",
        parent_sentinel="SENTINEL-B-PARENT-6666",
    )
    backend_name = "codex-cloud-restart"
    llm_config = LLMBackendConfiguration()
    llm_config.backends[backend_name] = BackendConfig(name=backend_name, backend_type="codex-cloud", environment_id="env-restart", attempts=1)
    config = AutomationConfig()

    def dispatch(issue_number: int, task_id: str) -> str:
        issue = _codex_issue(issue_number)
        with (
            patch("auto_coder.codex_cloud_client.get_llm_config", return_value=llm_config),
            patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True),
            patch("auto_coder.issue_processor.get_current_attempt", return_value=1),
            patch("auto_coder.issue_processor.get_commit_log", return_value="commit context"),
            patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as run_command,
        ):
            run_command.return_value = MagicMock(returncode=0, stdout=f"https://chatgpt.com/codex/tasks/{task_id}", stderr="")
            _process_issue_codex_cloud_mode("owner/repo", issue, config, MagicMock(), backend_name=backend_name)
        return run_command.call_args.args[0][-1]

    # --- Effective config A: initial dispatch for the session we will later repair.
    prompt_loader.clear_prompt_cache()
    with patch.object(prompt_loader, "DEFAULT_PROMPTS_PATH", config_a):
        initial_prompt_a = dispatch(1910, "task_e_restarta")
    assert initial_prompt_a.count("SENTINEL-A-SHORT-1111") == 1
    assert initial_prompt_a.count("SENTINEL-A-BOUNDARY-2222") == 1
    assert initial_prompt_a.count("SENTINEL-A-PARENT-3333") == 1

    # --- Simulate a restart that makes config B effective process-wide: a new
    # DEFAULT_PROMPTS_PATH plus a cleared cache, with no in-memory client or
    # controller carried over from the dispatch above.
    prompt_loader.clear_prompt_cache()
    with patch.object(prompt_loader, "DEFAULT_PROMPTS_PATH", config_b):
        # Repair the *original* (config-A-dispatched) session. The association
        # is read back fresh from disk through the same CloudManager boundary
        # every other test in this file uses — not a handcrafted substitute
        # and not anything carried over in memory from the dispatch above.
        resolved_binding = CloudManager("owner/repo").get_binding(1910)
        assert resolved_binding.task_id == "task_e_restarta"
        pr_data = {
            "number": 6100,
            "body": "Fixes #1910\n\nhttps://chatgpt.com/codex/tasks/task_e_restarta",
            "head": {"ref": "codex/issue-1910", "sha": "head-1"},
            "base": {"ref": "main"},
        }
        thread = ReviewThread(id="PRRT_restart", comments=[ReviewThreadComment(database_id=1, body="Newer repair detail", author_login="reviewer")])
        github_client = MagicMock()
        github_client.get_pr_comments.return_value = []
        github_client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(
            head_ref="codex/issue-1910",
            head_sha="head-1",
            base_ref="main",
        )
        with (
            patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "review.json"),
            patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
        ):
            _delegate_cloud_review_thread_repair("owner/repo", pr_data, github_client, (thread,))
        repair_prompt = send_followup.call_args.args[1]

        # Neither the old (A) nor the new (B) bootstrap is injected into the
        # continuation, while the newer repair detail and PR metadata are present.
        for sentinel in ("SENTINEL-A-SHORT-1111", "SENTINEL-A-BOUNDARY-2222", "SENTINEL-A-PARENT-3333", "SENTINEL-B-SHORT-4444", "SENTINEL-B-BOUNDARY-5555", "SENTINEL-B-PARENT-6666"):
            assert sentinel not in repair_prompt
        assert "Newer repair detail" in repair_prompt
        assert "codex/issue-1910" in repair_prompt

        # --- A brand-new task dispatched after the reload receives config B.
        initial_prompt_b = dispatch(1911, "task_e_restartb")
    assert initial_prompt_b.count("SENTINEL-B-SHORT-4444") == 1
    assert initial_prompt_b.count("SENTINEL-B-BOUNDARY-5555") == 1
    assert initial_prompt_b.count("SENTINEL-B-PARENT-6666") == 1
    assert "SENTINEL-A-SHORT-1111" not in initial_prompt_b


def test_pre_upgrade_session_without_process_local_prompt_history_is_still_a_continuation(cloud_home):
    """A resolved session created "before this change" (i.e. with no
    process-local record of what its initial prompt was — the repair path
    never consults one) remains a plain continuation: no transcript lookup,
    synthetic bootstrap, or new policy receipt is required or performed.
    """
    tmp_path = cloud_home
    # Persist the association directly, exactly as it would already exist on
    # disk for a session dispatched in a prior process, without replaying any
    # dispatch call in this test.
    CloudManager("owner/repo").ensure_binding(1920, CloudTaskBinding("codex-cloud", "task_e_preupgrade", "codex-legacy"))

    pr_data = {
        "number": 6101,
        "body": "Fixes #1920\n\nhttps://chatgpt.com/codex/tasks/task_e_preupgrade",
        "head": {"ref": "codex/issue-1920", "sha": "head-1"},
        "base": {"ref": "main"},
    }
    thread = ReviewThread(id="PRRT_legacy", comments=[ReviewThreadComment(database_id=1, body="Legacy session repair detail", author_login="reviewer")])
    github_client = MagicMock()
    github_client.get_pr_comments.return_value = []
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "review.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", pr_data, github_client, (thread,))

    assert result.delivered is True
    repair_prompt = send_followup.call_args.args[1]
    for marker in ALL_MARKERS:
        assert marker not in repair_prompt
    assert "Legacy session repair detail" in repair_prompt


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
