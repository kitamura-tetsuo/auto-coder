"""Scoped local correction verification, replay, and GitHub diagnostics."""

import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import format_adversarial_review_summary
from auto_coder.automation_config import AutomationConfig
from auto_coder.durable_repair_allowance import GenerationLifecycleState, RepairAllowanceLedger
from auto_coder.local_review_repair import LocalReviewRepairStore, admit_local_repair_allowance
from auto_coder.local_review_validation import LocalRepairVerificationStore, run_pending_local_repair_verification
from auto_coder.pr_processor import _review_feedback_identity
from auto_coder.review_thread_validation import ClaimedReviewThread
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment
from tests.test_local_review_repair import _git, _repository, _request


@pytest.fixture
def verification_case(tmp_path, monkeypatch, _use_custom_subprocess_mock):
    repository, baseline = _repository(tmp_path)
    (repository / "tracked.txt").write_text("corrective change\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "correct original findings")
    head = _git(repository, "rev-parse", "HEAD")
    monkeypatch.chdir(repository)
    threads = tuple(ClaimedReviewThread(thread_id=f"target-{index}", root_comment_database_id=index, root_author_login="auto-coder-reviewer[bot]", original_finding=f"### Auto-Coder adversarial finding\nOriginal counterexample {index}") for index in (1, 2))
    identities = tuple(_review_feedback_identity("owner/repo#42:local:", ReviewThread(id=thread.thread_id, comments=[ReviewThreadComment(database_id=thread.root_comment_database_id)]), 0) for thread in threads)
    request = replace(_request(feedback=identities), head_sha=baseline)
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, _ = admit_local_repair_allowance(request, ledger)
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    claim = store.admit(request)
    assert store.transition(request, claim, "awaiting_validation", result_sha=head)
    authority.mark_invocation()
    authority.mark_completion(code_changed=True, evidence="published corrective commit")
    return repository, head, threads, identities, request, ledger, store, authority


def _disposition(thread_id, status):
    return {"thread_id": thread_id, "status": status, "rationale": "Original boundary independently checked", "evidence": "tracked.txt:1 and focused regression results"}


def _invoke(case, path, publish, targets=None, head_is_current=None):
    repository, head, threads, _identities, _request_value, ledger, store, _authority = case
    return run_pending_local_repair_verification(
        "owner/repo",
        42,
        head,
        select_threads=lambda: threads if targets is None else targets,
        linked_issue_contract=lambda: "REQ-001: preserve the original invariant",
        worktree=lambda: nullcontext(repository),
        publish=publish,
        head_is_current=head_is_current,
        ledger=ledger,
        repair_store=store,
        verification_store=LocalRepairVerificationStore(path),
        backend_manager=MagicMock(),
    )


def test_pending_scope_partial_result_is_visible_and_not_repeated_after_restart(verification_case, tmp_path):
    case = verification_case
    _repository_value, head, threads, identities, _request_value, ledger, _store, authority = case
    response = json.dumps({"thread_dispositions": [_disposition("target-1", "STILL_VALID")]})
    publish = MagicMock(return_value=True)
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response) as invoke:
        first = _invoke(case, tmp_path / "verification.sqlite3", publish)
        second = _invoke(case, tmp_path / "verification.sqlite3", publish)
    invoke.assert_called_once()
    publish.assert_called_once()
    prompt = invoke.call_args.args[0]
    assert "Verify ONLY" in prompt
    assert "NOT a new PR-wide adversarial validation" in prompt
    assert "ADDRESSED requires a committed focused regression test" in prompt
    assert "Original counterexample 1" in prompt and "Original counterexample 2" in prompt
    assert "### Pending local correction target: target-1" in prompt
    assert "### Claimed-addressed review thread:" not in prompt
    assert "### Forced adversarial-validation revalidation" not in prompt
    assert invoke.call_args.kwargs["is_noedit"] is True
    assert [(target.blocker_id, target.thread_id) for target in first.unverified_local_repairs] == [(identities[1], "target-2")]
    assert second.unverified_local_repairs == first.unverified_local_repairs
    body = format_adversarial_review_summary(first, head)
    assert "Pending local corrections NOT verified" in body
    assert "Reviewer omitted the required disposition" in body
    assert "target-2" in body and identities[1] in body
    assert "target-1`: STILL_VALID" in body
    assert "auto-coder-adversarial-validation:" not in body
    assert "auto-coder-local-repair-validation:" in body
    snapshot = ledger.get_snapshot("https://api.github.com", "owner/repo", 42)
    assert snapshot.get_generation(authority.generation_id).lifecycle_state == GenerationLifecycleState.PENDING_REVALIDATION
    assert snapshot.get_blocker_allowance(identities[0]).failed_count == 1
    assert snapshot.get_blocker_allowance(identities[1]).failed_count == 0


def test_only_available_pending_roots_are_verified_and_missing_root_is_reported(verification_case, tmp_path):
    case = verification_case
    response = json.dumps({"thread_dispositions": [_disposition("target-2", "ADDRESSED")]})
    publish = MagicMock(return_value=True)
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response) as invoke:
        result = _invoke(case, tmp_path / "verification.sqlite3", publish, targets=(case[2][1],))
    assert "Original counterexample 1" not in invoke.call_args.args[0]
    assert [(target.blocker_id, target.thread_id) for target in result.unverified_local_repairs] == [(case[3][0], "")]
    assert "missing, truncated, or unauthenticated" in result.unverified_local_repairs[0].reason
    snapshot = case[5].get_snapshot("https://api.github.com", "owner/repo", 42)
    assert tuple(item.blocker_id for item in snapshot.get_outstanding_generation().settlements) == (case[3][1],)
    publish.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        '{"findings": []}',
        '{"thread_dispositions": []}',
        json.dumps({"thread_dispositions": [_disposition("unrelated", "STILL_VALID")]}),
        json.dumps({"thread_dispositions": [_disposition("target-1", "ADDRESSED"), _disposition("target-1", "STILL_VALID")]}),
        json.dumps({"thread_dispositions": [_disposition("target-1", "INCONCLUSIVE"), _disposition("target-2", "INCONCLUSIVE")]}),
    ],
)
def test_incomplete_verification_is_published_as_unverified_not_still_valid(verification_case, tmp_path, response):
    publish = MagicMock(return_value=True)
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response):
        result = _invoke(verification_case, tmp_path / "verification.sqlite3", publish)
    assert {item.blocker_id for item in result.unverified_local_repairs} == set(verification_case[3])
    assert result.allows_auto_merge is False
    body = format_adversarial_review_summary(result, verification_case[1])
    assert "local repair verification: INCOMPLETE" in body
    assert "Pending local corrections NOT verified" in body
    publish.assert_called_once()
    generation = verification_case[5].get_snapshot("https://api.github.com", "owner/repo", 42).get_outstanding_generation()
    assert generation.settlements == ()


def test_completed_scope_settles_corrected_and_failed_roots_without_full_pr_pass(verification_case, tmp_path):
    response = json.dumps({"thread_dispositions": [_disposition("target-2", "ADDRESSED"), _disposition("target-1", "STILL_VALID")]})
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response):
        result = _invoke(verification_case, tmp_path / "verification.sqlite3", MagicMock(return_value=True))
    assert result.unverified_local_repairs == []
    assert result.is_pass is False
    assert "local repair verification: COMPLETE" in format_adversarial_review_summary(result, verification_case[1])
    snapshot = verification_case[5].get_snapshot("https://api.github.com", "owner/repo", 42)
    assert snapshot.get_outstanding_generation() is None
    assert snapshot.get_blocker_allowance(verification_case[3][0]).failed_count == 1
    assert snapshot.get_blocker_allowance(verification_case[3][1]).failed_count == 0


@pytest.mark.parametrize("reason", ["Focused verification failed: test exit code 2", "pending correction roots could not be acquired"])
def test_verification_execution_failure_is_visible_for_every_pending_target(verification_case, tmp_path, reason):
    publish = MagicMock(return_value=True)
    path = tmp_path / "verification.sqlite3"
    with patch("auto_coder.local_review_validation.run_llm_prompt", side_effect=RuntimeError(reason)) as invoke:
        result = _invoke(verification_case, path, publish)
        _invoke(verification_case, path, publish)
    invoke.assert_called_once()
    publish.assert_called_once()
    assert result.thread_dispositions == []
    assert [(item.blocker_id, item.thread_id) for item in result.unverified_local_repairs] == list(zip(verification_case[3], ("target-1", "target-2")))
    body = format_adversarial_review_summary(result, verification_case[1])
    assert "local repair verification: INCOMPLETE" in body
    assert reason in body
    assert "target-1" in body and "target-2" in body
    assert "`: STILL_VALID" not in body
    snapshot = verification_case[5].get_snapshot("https://api.github.com", "owner/repo", 42)
    assert snapshot.get_generation(verification_case[7].generation_id).settlements == ()


def test_missing_roots_before_backend_entry_can_resume_at_same_head(verification_case, tmp_path):
    publish = MagicMock(return_value=True)
    path = tmp_path / "verification.sqlite3"
    response = json.dumps({"thread_dispositions": [_disposition(target.thread_id, "ADDRESSED") for target in verification_case[2]]})
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response) as invoke:
        missing = _invoke(verification_case, path, publish, targets=())
        _invoke(verification_case, path, publish, targets=())
        invoke.assert_not_called()
        assert publish.call_count == 1
        recovered = _invoke(verification_case, path, publish)
    assert {item.reason for item in missing.unverified_local_repairs} == {"pending correction roots could not be acquired"}
    invoke.assert_called_once()
    assert publish.call_count == 2
    assert recovered.unverified_local_repairs == []
    assert verification_case[5].get_snapshot("https://api.github.com", "owner/repo", 42).get_outstanding_generation() is None


def test_missing_root_recovery_claim_has_only_one_owner(verification_case, tmp_path):
    path = tmp_path / "verification.sqlite3"
    _invoke(verification_case, path, MagicMock(return_value=True), targets=())
    store = LocalRepairVerificationStore(path)
    generation_id, head = verification_case[7].generation_id, verification_case[1]
    checkpoint = store.get(generation_id, head)
    assert store.reclaim_missing_roots(generation_id, head, checkpoint) is True
    assert LocalRepairVerificationStore(path).reclaim_missing_roots(generation_id, head, checkpoint) is False
    assert store.get(generation_id, head).phase == "executing"
    assert store.reserve_publication(generation_id, head, checkpoint.response) is False
    assert store.mark_published(generation_id, head, checkpoint.response) is False
    assert store.get(generation_id, head).published is False


def test_no_change_completion_never_revalidates_unchanged_commit(verification_case, tmp_path):
    case = verification_case
    _repository_value, _head, _threads, _ids, request, _ledger, store, _authority = case
    record = store.get(request)
    from auto_coder.local_review_repair import LocalReviewRepairClaim

    claim = LocalReviewRepairClaim(False, record.phase, record.attempt_id, record.incarnation)
    assert store.transition(request, claim, "completed_no_change", result_sha=request.head_sha)
    case = (case[0], request.head_sha, *case[2:])
    publish = MagicMock(return_value=True)
    with patch("auto_coder.local_review_validation.run_llm_prompt") as invoke:
        result = _invoke(case, tmp_path / "verification.sqlite3", publish)
        _invoke(case, tmp_path / "verification.sqlite3", publish)
    invoke.assert_not_called()
    publish.assert_called_once()
    assert len(result.unverified_local_repairs) == 2
    assert "produced no new commit" in result.unverified_local_repairs[0].reason


def test_no_change_completion_can_verify_a_later_new_commit(verification_case, tmp_path):
    case = verification_case
    from auto_coder.local_review_repair import LocalReviewRepairClaim

    request, store = case[4], case[6]
    record = store.get(request)
    claim = LocalReviewRepairClaim(False, record.phase, record.attempt_id, record.incarnation)
    assert store.transition(request, claim, "completed_no_change", result_sha=request.head_sha)
    response = json.dumps({"thread_dispositions": [_disposition(target.thread_id, "ADDRESSED") for target in case[2]]})
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response) as invoke:
        result = _invoke(case, tmp_path / "verification.sqlite3", MagicMock(return_value=True))
    invoke.assert_called_once()
    assert not result.unverified_local_repairs
    assert case[5].get_snapshot("https://api.github.com", "owner/repo", 42).get_outstanding_generation() is None


def test_head_change_after_publication_retains_pending_settlement_without_reinvocation(verification_case, tmp_path):
    case = verification_case
    response = json.dumps({"thread_dispositions": [_disposition(target.thread_id, "ADDRESSED") for target in case[2]]})
    publish = MagicMock(return_value=True)
    path = tmp_path / "verification.sqlite3"
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response) as invoke:
        with pytest.raises(RuntimeError, match="head changed before verification settlement"):
            _invoke(case, path, publish, head_is_current=lambda: False)
        snapshot = case[5].get_snapshot("https://api.github.com", "owner/repo", 42)
        assert not snapshot.get_generation(case[7].generation_id).settlements
        _invoke(case, path, publish, head_is_current=lambda: True)
    invoke.assert_called_once()
    publish.assert_called_once()
    assert case[5].get_snapshot("https://api.github.com", "owner/repo", 42).get_outstanding_generation() is None


def test_settlement_cannot_apply_a_report_to_a_different_generation(verification_case):
    from auto_coder.durable_repair_allowance import ValidationObservation
    from auto_coder.local_review_repair import settle_local_review_repair_validation

    case = verification_case
    with pytest.raises(RuntimeError, match="generation changed"):
        settle_local_review_repair_validation("owner/repo", 42, case[1], (ValidationObservation(case[3][0], True, validation_seq=1, evidence="original generation report"),), ledger=case[5], store=case[6], expected_generation_id="different-generation")
    snapshot = case[5].get_snapshot("https://api.github.com", "owner/repo", 42)
    assert snapshot.get_generation(case[7].generation_id).settlements == ()
    assert snapshot.get_blocker_allowance(case[3][0]).failed_count == 0


def test_unconfirmed_publication_reconciles_without_reinvocation_or_blind_resend(verification_case, tmp_path):
    response = json.dumps({"thread_dispositions": [_disposition("target-1", "STILL_VALID")]})
    publish = MagicMock(side_effect=[False, True])
    with patch("auto_coder.local_review_validation.run_llm_prompt", return_value=response) as invoke:
        with pytest.raises(RuntimeError, match="publication is unconfirmed"):
            _invoke(verification_case, tmp_path / "verification.sqlite3", publish)
        assert verification_case[5].get_snapshot("https://api.github.com", "owner/repo", 42).get_outstanding_generation().settlements == ()
        _invoke(verification_case, tmp_path / "verification.sqlite3", publish)
    invoke.assert_called_once()
    assert [call.args[1] for call in publish.call_args_list] == [True, False]


def test_interrupted_invocation_reports_uncertainty_without_reinvocation(verification_case, tmp_path):
    database = tmp_path / "verification.sqlite3"
    store = LocalRepairVerificationStore(database)
    assert store.claim(verification_case[7].generation_id, verification_case[1]) is True
    publish = MagicMock(return_value=True)
    with patch("auto_coder.local_review_validation.run_llm_prompt") as invoke:
        result = _invoke(verification_case, database, publish)
        _invoke(verification_case, database, publish)
    invoke.assert_not_called()
    publish.assert_called_once()
    assert "executing or indeterminate" in result.unverified_local_repairs[0].reason
    assert verification_case[5].get_snapshot("https://api.github.com", "owner/repo", 42).get_outstanding_generation().settlements == ()


def test_pending_diagnostic_cannot_absorb_later_completed_report(verification_case, tmp_path):
    from auto_coder.adversarial_validator import AdversarialValidationResult, ReviewThreadDisposition, UnverifiedLocalRepair

    database = tmp_path / "verification.sqlite3"
    store = LocalRepairVerificationStore(database)
    generation_id = verification_case[7].generation_id
    head = verification_case[1]
    assert store.claim(generation_id, head)
    publish = MagicMock(return_value=True)
    with patch("auto_coder.local_review_validation.run_llm_prompt") as invoke:
        pending = _invoke(verification_case, database, publish)
        completed = AdversarialValidationResult(
            result="INCONCLUSIVE", local_repair_generation_id=generation_id, thread_dispositions=[ReviewThreadDisposition(**_disposition("target-1", "STILL_VALID"))], unverified_local_repairs=[UnverifiedLocalRepair(verification_case[3][1], "target-2", "missing disposition")]
        )
        store.complete(generation_id, head, completed)
        final = _invoke(verification_case, database, publish)
    invoke.assert_not_called()
    assert publish.call_count == 2
    assert [call.args[1] for call in publish.call_args_list] == [True, True]
    assert format_adversarial_review_summary(pending, head).splitlines()[0] != format_adversarial_review_summary(final, head).splitlines()[0]


@pytest.mark.parametrize("threads_resolved", [False, True])
@pytest.mark.parametrize("ordinary_roots", [False, True])
def test_production_pending_lane_excludes_unrelated_threads_and_reports_missing_target_in_mounted_view(verification_case, monkeypatch, threads_resolved, ordinary_roots):
    from types import SimpleNamespace

    from auto_coder.automation_config import ProcessedPRResult
    from auto_coder.execution_trace import Outcome, TraceCollector, get_trace_collector
    from auto_coder.github_app_reviewer import ReviewPublicationResult
    from auto_coder.pr_processor import ClaimedReviewThreadGateState, _handle_pr_merge
    from auto_coder.util.github_action import GitHubActionsStatusResult
    from tests.test_dashboard_observability import _mounted_detail

    repository, head, targets, identities, _request_value, ledger, store, _authority = verification_case
    client = MagicMock()
    roots = [
        ReviewThread(
            id=target.thread_id,
            is_resolved=threads_resolved,
            comments=[ReviewThreadComment(database_id=target.root_comment_database_id, author_login="human-reviewer" if ordinary_roots else target.root_author_login, body=target.original_finding.replace("### Auto-Coder adversarial finding\n", "") if ordinary_roots else target.original_finding)],
        )
        for target in targets
    ]
    unrelated = ReviewThread(id="unrelated", comments=[ReviewThreadComment(database_id=99, author_login="auto-coder-reviewer[bot]", body="### Auto-Coder adversarial finding\nUnrelated boundary must not enter this verification")])
    client.get_pr_review_threads_strict.return_value = roots + [unrelated]
    client.get_pull_request_head_sha_strict.return_value = head
    client.get_pr_reviews_strict.return_value = []
    pr_data = {"number": 42, "body": "<!-- auto-coder:local-llm -->\nFixes #7", "labels": [], "head": {"ref": "repair", "sha": head}, "base": {"ref": "main"}}
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = True
    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = get_trace_collector()
    gate_state = ClaimedReviewThreadGateState() if threads_resolved else ClaimedReviewThreadGateState(unresolved=tuple(roots + [unrelated]), blocking_unresolved=tuple(roots + [unrelated]), has_blocking_unresolved=True)
    processing_status = ProcessedPRResult(pr_data=pr_data)
    with (
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=gate_state),
        patch("auto_coder.durable_repair_allowance.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.local_review_validation.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.local_review_validation.LocalReviewRepairStore", return_value=store),
        patch("auto_coder.local_review_validation.LocalRepairVerificationStore", return_value=LocalRepairVerificationStore(repository.parent / "verification.sqlite3")),
        patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability", return_value=SimpleNamespace(backend_manager=MagicMock())),
        patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-001: original invariant"),
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", side_effect=lambda *args: nullcontext(repository)),
        patch("auto_coder.local_review_validation.run_llm_prompt", return_value=json.dumps({"thread_dispositions": [_disposition("target-1", "STILL_VALID")]})) as scoped,
        patch("auto_coder.pr_processor.run_adversarial_validation") as broad,
        patch("auto_coder.local_review_repair.execute_local_review_repair") as repair,
        patch("auto_coder.pr_processor.publish_adversarial_review", return_value=ReviewPublicationResult(True, "COMMENT", "")) as publish,
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        with collector.start_execution("owner/repo", "pr", 42, origin="worker"):
            first = _handle_pr_merge(client, "owner/repo", pr_data, config, {}, processing_status=processing_status)
        _handle_pr_merge(client, "owner/repo", pr_data, config, {}, force_adversarial_validation=True)
    assert scoped.call_count == 1, first
    broad.assert_not_called()
    repair.assert_not_called()
    merge.assert_not_called()
    publish.assert_called_once()
    assert "Unrelated boundary" not in scoped.call_args.args[0]
    assert "Original counterexample 1" in scoped.call_args.args[0]
    assert "Original counterexample 2" in scoped.call_args.args[0]
    body = format_adversarial_review_summary(publish.call_args.args[3], head)
    assert "Pending local corrections NOT verified" in body and "target-2" in body and identities[1] in body
    assert any("no full PR validation was started" in action for action in first)
    assert processing_status.target_reason == "Reviewer omitted the required disposition; this target was not verified."
    assert any(processing_status.target_reason in action for action in first)
    snapshot = collector.get_snapshot(repository="owner/repo", item_type="pr", item_number=42)
    events = [event for event in snapshot.events if event.stage_id == "pr.repair-delegation"]
    assert events[-1].outcome == Outcome.BLOCKED.value
    assert events[-1].facts["effect"] == "local-validation-scoped"
    assert events[-1].facts["unverified_count"] == 1
    assert events[-1].facts["unverified_targets"][0]["thread_id"] == "target-2"
    with patch("auto_coder.dashboard.ui") as ui:
        _mounted_detail(ui, "pr", 42)
    assert any("local-validation-scoped" in str(call) and "target-2" in str(call) for call in ui.table.call_args_list)


def test_scoped_human_root_verification_does_not_authorize_thread_resolution(verification_case):
    from auto_coder.adversarial_validator import AdversarialValidationResult, ReviewThreadDisposition
    from auto_coder.automation_config import ProcessedPRResult
    from auto_coder.pr_processor import ClaimedReviewThreadGateState, _verify_pending_local_correction

    _repo, head, targets, _identities, _request_value, ledger, _store, authority = verification_case
    client = MagicMock()
    client.get_pull_request_head_sha_strict.return_value = head
    roots = [ReviewThread(id=target.thread_id, is_resolved=True, comments=[ReviewThreadComment(database_id=target.root_comment_database_id, author_login="human-reviewer", body="Please correct this original boundary")]) for target in targets]
    client.get_pr_review_threads_strict.return_value = roots
    result = AdversarialValidationResult(result="INCONCLUSIVE", local_repair_generation_id=authority.generation_id, thread_dispositions=[ReviewThreadDisposition(**_disposition(target.thread_id, "ADDRESSED")) for target in targets])

    def verify(*args, **kwargs):
        selected = kwargs["select_threads"]()
        assert [thread.thread_id for thread in selected] == [thread.thread_id for thread in targets]
        assert all(thread.original_finding == "Please correct this original boundary" for thread in selected)
        return result

    pr_data = {"number": 42, "head": {"sha": head}}
    status = ProcessedPRResult(pr_data=pr_data)
    actions = []
    with (
        patch("auto_coder.durable_repair_allowance.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=ClaimedReviewThreadGateState()),
        patch("auto_coder.local_review_validation.run_pending_local_repair_verification", side_effect=verify),
        patch("auto_coder.pr_processor.resolve_addressed_review_threads") as close,
    ):
        _verify_pending_local_correction(client, "owner/repo", pr_data, AutomationConfig(), actions, status)
    close.assert_not_called()
    client.resolve_review_thread.assert_not_called()
    assert status.target_reason == "Scoped local correction verification completed; full PR validation is still required"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("status", ["NEEDS_FIX", "NEEDS_TESTS"])
def test_same_commit_actionable_validation_routes_to_local_repair_even_with_force(monkeypatch, status, force):
    from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult, format_adversarial_finding_comment, format_adversarial_validation_comment
    from auto_coder.local_review_repair import LocalReviewRepairOutcome
    from auto_coder.pr_processor import ClaimedReviewThreadGateState, ReviewRepairRouteDecision, ReviewRepairRouteDisposition, _handle_pr_merge
    from auto_coder.util.gh_cache import PullRequestRoutingMetadata
    from auto_coder.util.github_action import GitHubActionsStatusResult

    head = "unchanged-commit"
    finding = AdversarialValidationFinding(violated_requirement="REQ-001", counterexample="Original demonstrated counterexample")
    saved = AdversarialValidationResult(result=status, summary="Unresolved original validation", findings=[finding])
    thread = ReviewThread(id="original", comments=[ReviewThreadComment(database_id=1, body=format_adversarial_finding_comment(finding), author_login="auto-coder-reviewer[bot]")])
    client = MagicMock()
    client.get_pr_review_threads_strict.return_value = [thread]
    client.get_pr_reviews_strict.return_value = [{"body": format_adversarial_validation_comment(saved, head), "user": {"login": "auto-coder-reviewer[bot]"}, "commit_id": head}]
    pr_data = {"number": 42, "body": "<!-- auto-coder:local-llm -->\nFixes #7", "labels": [], "head": {"ref": "repair", "sha": head}, "base": {"ref": "main"}}
    route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED, "local", PullRequestRoutingMetadata("https://api.github.com", "owner/repo", 42, "open", pr_data["body"], "owner/repo", "repair", head))
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = True
    with (
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True)),
        patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-001: original invariant"),
        patch("auto_coder.local_review_repair.admit_local_repair_allowance", return_value=(object(), "")),
        patch("auto_coder.local_review_repair.execute_local_review_repair", return_value=LocalReviewRepairOutcome("executing", "existing local correction is active")) as repair,
        patch("auto_coder.pr_processor.run_adversarial_validation") as broad,
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {}, force_adversarial_validation=force)
    repair.assert_called_once()
    broad.assert_not_called()
    merge.assert_not_called()
    assert "Original demonstrated counterexample" in repair.call_args.args[0].prompt
    assert repair.call_args.args[0].head_sha == head
    assert any(f"Reused unresolved {status}" in action for action in actions)
