"""Production-path regression for applying review adjudication effects.

Builds real ``AdjudicationContextStore``/``AdjudicationLedger`` state the same
way the authoritative GitHub reader (#2018) does, then drives
``pr_processor._apply_review_adjudication_effects`` against a fake GitHub
client and the real cloud-task-origin resolution path (mocking only the
external ``CloudManager``/``CodexCloudClient`` boundary, exactly like the
existing ``_delegate_cloud_review_thread_repair`` regressions in
``tests/test_codex_cloud_pr_review_flow.py``), so the whole
read -> plan -> deliver/retire -> journal path is exercised end to end.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import format_test_oracle_gap_comment
from auto_coder.cloud_manager import CloudTaskBinding
from auto_coder.pr_processor import (
    _adjudication_context_store_path,
    _adjudication_effects_store_path,
    _apply_review_adjudication_effects,
    _delegate_cloud_review_thread_repair,
)
from auto_coder.review_adjudication import Decision, render_decision
from auto_coder.review_adjudication_github import AdjudicationContextStore, IssueEvidence, PullRequestBinding, build_issue_contracts, new_context
from auto_coder.reviewer_session_registry import ReviewerSessionRegistry, TestOracleGap
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment

BODY = """## Objective

Keep the boundary exact.

## Requirements

REQ-001: Preserve the raw value.
"""

ADJUDICATOR_ID = 8
ROOT_AUTHOR_ID = 7
HEAD_SHA = "a" * 40
REPO = "owner/repo"
PR_NUMBER = 4001


def _pr_data() -> dict:
    return {
        "number": PR_NUMBER,
        "head": {"ref": "work-branch", "sha": HEAD_SHA},
        "base": {"ref": "main", "sha": "b" * 40},
        "labels": [],
        "user": {"login": "maintainer"},
    }


@pytest.fixture(autouse=True)
def _authorized_allowlists():
    """Re-proving current authority now reads the effective config allowlists.

    Production resolves these from ``[github].pr_review_allowlist`` /
    ``[github].review_adjudicator_allowlist``; tests supply the same
    root/adjudicator IDs the fixtures below build ledgers with.
    """
    with (
        patch("auto_coder.pr_processor.get_pr_review_allowlist_from_config", return_value=[ROOT_AUTHOR_ID]),
        patch("auto_coder.pr_processor.get_review_adjudicator_allowlist_from_config", return_value=[ADJUDICATOR_ID]),
    ):
        yield


def _github_client(thread: ReviewThread) -> MagicMock:
    client = MagicMock()
    client.get_pr_review_threads_strict.return_value = [thread]
    client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref="work-branch", head_sha=HEAD_SHA, base_ref="main")
    client.get_pull_request_metadata_strict.return_value = {"head": {"sha": HEAD_SHA}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}
    return client


def _register_decision(root_body: str, verdict: str, directive: str, thread_id: str = "T1", root_id: int = 10) -> ReviewThread:
    thread = ReviewThread(id=thread_id, comments=[ReviewThreadComment(root_id, root_body, "bot", ROOT_AUTHOR_ID, "Bot", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")])
    contracts = build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])
    binding = PullRequestBinding(3, REPO, PR_NUMBER, HEAD_SHA, "b" * 40, "main")
    context = new_context(binding, thread, contracts)
    decision = Decision(str(uuid.uuid4()), context.context_id, HEAD_SHA, context.contract_digest, verdict, directive, (), "Remove the accidental change; keep the worker correction.", "chatgpt-assisted")
    thread.comments.append(ReviewThreadComment(root_id + 1, render_decision(decision), "human", ADJUDICATOR_ID, "User", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", root_id))
    store = AdjudicationContextStore(_adjudication_context_store_path())
    ledger = store.register(context, "r1")
    from auto_coder.review_adjudication_github import reconcile_thread

    result = reconcile_thread(ledger, thread, [ADJUDICATOR_ID], [ROOT_AUTHOR_ID])
    store.save(ledger, "r1")
    assert result.status.value == "APPLICABLE"
    assert result.verdict == verdict
    return thread


def test_upheld_adjudication_delivers_bounded_repair_via_existing_cloud_task() -> None:
    thread = _register_decision("This misses the empty-input case", "UPHOLD", "FIX")
    github_client = _github_client(thread)

    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="codex-cloud", task_id="task_e_adj1")),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        actions, force_revalidation = _apply_review_adjudication_effects(REPO, PR_NUMBER, _pr_data(), github_client)

    assert any("task_e_adj1" in action for action in actions)
    assert send_followup.call_count == 1
    prompt = send_followup.call_args[0][1]
    assert "Remove the accidental change; keep the worker correction." in prompt
    assert "auto-coder-review-adjudication:v1" not in prompt
    github_client.resolve_review_thread.assert_not_called()
    assert force_revalidation is False

    # Same generation, already delivered: no second follow-up.
    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="codex-cloud", task_id="task_e_adj1")),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup_again,
    ):
        _apply_review_adjudication_effects(REPO, PR_NUMBER, _pr_data(), github_client)
    assert send_followup_again.call_count == 0


def test_upheld_adjudication_without_a_cloud_task_stays_pending_and_forces_revalidation() -> None:
    thread = _register_decision("This misses the empty-input case", "UPHOLD", "FIX")
    github_client = _github_client(thread)

    with patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None):
        actions, force_revalidation = _apply_review_adjudication_effects(REPO, PR_NUMBER, _pr_data(), github_client)

    assert any("was not delivered" in action for action in actions)
    assert force_revalidation is True


def test_overruled_adjudication_retires_gap_and_resolves_thread() -> None:
    gap = TestOracleGap(
        gap_id="TOG-integ001",
        requirement_id="REQ-001",
        requirement_text="Preserve the raw value.",
        authoritative_boundary="boundary",
        invariant="invariant",
        plausible_incorrect_implementation="impl",
        why_tests_still_pass="reason",
        material_consequence="consequence",
        focused_regression_scenario="scenario",
        anchor_path="src/a.py",
        status="OPEN",
    )
    registry = ReviewerSessionRegistry()
    from auto_coder.reviewer_session_registry import ReviewerSession

    registry.save(ReviewerSession(repository=REPO, pr_number=PR_NUMBER, backend_name="codex", backend_type="codex", model_name="strong", session_id="s1", last_head_sha=HEAD_SHA, test_oracle_gaps=[gap]))

    root_body = format_test_oracle_gap_comment(gap)
    thread = _register_decision(root_body, "OVERRULE", "NO_CHANGE")
    github_client = _github_client(thread)

    actions, force_revalidation = _apply_review_adjudication_effects(REPO, PR_NUMBER, _pr_data(), github_client)

    assert any("Retired an overruled finding" in action for action in actions)
    github_client.reply_to_review_thread.assert_called_once()
    reply_body = github_client.reply_to_review_thread.call_args[0][3]
    assert "auto-coder-review-adjudication-overruled:v1" in reply_body
    github_client.resolve_review_thread.assert_called_once_with("T1")

    reloaded = registry.get(REPO, PR_NUMBER, "codex", "codex", "strong")
    assert reloaded is not None
    reloaded_gap = next(item for item in reloaded.test_oracle_gaps if item.gap_id == "TOG-integ001")
    assert reloaded_gap.status == "INVALID"
    assert force_revalidation is False


def test_revoked_adjudicator_authorization_blocks_a_previously_applicable_overrule() -> None:
    """A persisted APPLICABLE tip is not evidence once its authority is gone.

    Reproduces the exact counterexample from PR #2114 review: authority is
    revoked (the adjudicator allowlist is emptied) after the decision was
    accepted, and normal processing runs again at the unchanged head. No
    reply, resolve, or gap mutation may occur (REQ-001, REQ-002, REQ-011,
    REQ-014).
    """
    gap = TestOracleGap(
        gap_id="TOG-revoked01",
        requirement_id="REQ-001",
        requirement_text="Preserve the raw value.",
        authoritative_boundary="boundary",
        invariant="invariant",
        plausible_incorrect_implementation="impl",
        why_tests_still_pass="reason",
        material_consequence="consequence",
        focused_regression_scenario="scenario",
        anchor_path="src/a.py",
        status="OPEN",
    )
    from auto_coder.reviewer_session_registry import ReviewerSession

    registry = ReviewerSessionRegistry()
    registry.save(ReviewerSession(repository=REPO, pr_number=PR_NUMBER, backend_name="codex", backend_type="codex", model_name="strong", session_id="s1", last_head_sha=HEAD_SHA, test_oracle_gaps=[gap]))

    root_body = format_test_oracle_gap_comment(gap)
    thread = _register_decision(root_body, "OVERRULE", "NO_CHANGE")
    github_client = _github_client(thread)

    with patch("auto_coder.pr_processor.get_review_adjudicator_allowlist_from_config", return_value=[]):
        actions, force_revalidation = _apply_review_adjudication_effects(REPO, PR_NUMBER, _pr_data(), github_client)

    assert not any("Retired an overruled finding" in action for action in actions)
    github_client.reply_to_review_thread.assert_not_called()
    github_client.resolve_review_thread.assert_not_called()
    reloaded = registry.get(REPO, PR_NUMBER, "codex", "codex", "strong")
    assert reloaded is not None
    reloaded_gap = next(item for item in reloaded.test_oracle_gaps if item.gap_id == "TOG-revoked01")
    assert reloaded_gap.status == "OPEN"

    # The revocation is durable: a later pass with authority restored cannot
    # revive the old context (REQ-011, REQ-014 "H1 -> observed H2 -> H1").
    actions_again, _ = _apply_review_adjudication_effects(REPO, PR_NUMBER, _pr_data(), github_client)
    assert not any("Retired an overruled finding" in action for action in actions_again)
    github_client.resolve_review_thread.assert_not_called()


def test_edited_accepted_source_blocks_reliance_on_the_stale_decision() -> None:
    """An accepted decision reply edited after acceptance retires the context."""
    thread = _register_decision("This misses the empty-input case", "UPHOLD", "FIX")
    # Simulate the accepted reply being edited: same comment ID, different body/revision.
    thread.comments[1] = ReviewThreadComment(thread.comments[1].database_id, "edited to say something else entirely", "human", ADJUDICATOR_ID, "User", thread.comments[1].created_at, "2026-01-03T00:00:00Z", thread.comments[1].in_reply_to_id)
    github_client = _github_client(thread)

    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="codex-cloud", task_id="task_e_adj1")),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        actions, _ = _apply_review_adjudication_effects(REPO, PR_NUMBER, _pr_data(), github_client)

    assert send_followup.call_count == 0
    assert not any("task_e_adj1" in action for action in actions)


def test_adjudication_envelope_reply_is_excluded_from_generic_cloud_feedback() -> None:
    decision = Decision(str(uuid.uuid4()), str(uuid.uuid4()), HEAD_SHA, "c" * 64, "UPHOLD", "FIX", (), "bounded correction", "chatgpt-assisted")
    thread = ReviewThread(
        id="T2",
        comments=[
            ReviewThreadComment(20, "This misses the empty-input case", "bot", ROOT_AUTHOR_ID, "Bot", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ReviewThreadComment(21, render_decision(decision), "human", ADJUDICATOR_ID, "User", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", 20),
        ],
    )
    github_client = _github_client(thread)
    github_client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref="work-branch", head_sha=HEAD_SHA, base_ref="main")

    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="codex-cloud", task_id="task_e_generic1")),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair(REPO, _pr_data(), github_client=github_client, unresolved_threads=(thread,))

    assert send_followup.call_count == 1
    prompt = send_followup.call_args[0][1]
    assert "auto-coder-review-adjudication:v1" not in prompt
    assert "This misses the empty-input case" in prompt
