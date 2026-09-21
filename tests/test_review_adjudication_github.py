import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auto_coder.review_adjudication import AdjudicationStatus, Decision, render_decision
from auto_coder.review_adjudication_github import AdjudicationContextStore, IssueEvidence, PullRequestBinding, ReviewAdjudicationService, build_issue_contracts, new_context, publish_context, reconcile_thread, render_context_projection
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

BODY = """## Objective

Keep π exact.

## Requirements

REQ-001: Preserve the raw value.
"""


def _thread() -> ReviewThread:
    return ReviewThread(id="T1", comments=[ReviewThreadComment(10, "finding", "bot", 7, "Bot", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")])


def _publication_arguments(ledger):
    return [8], ledger.current(None, None, "authoritative thread reconciled"), "observation-1"


def test_context_registration_is_restart_safe_and_singleton(tmp_path: Path) -> None:
    contracts = build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])
    binding = PullRequestBinding(3, "o/r", 4, "a" * 40, "b" * 40, "main")
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    registered = store.register(new_context(binding, _thread(), contracts), "observation-1")
    second = store.register(new_context(binding, _thread(), contracts), "observation-2")
    assert second.context.context_id == registered.context.context_id


def test_ordinary_discussion_has_no_authority(tmp_path: Path) -> None:
    contracts = build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])
    context = new_context(PullRequestBinding(3, "o/r", 4, "a" * 40, "b" * 40, "main"), _thread(), contracts)
    ledger = AdjudicationContextStore(tmp_path / "state.sqlite").register(context, "r1")
    thread = _thread()
    thread.comments.append(ReviewThreadComment(11, "LGTM", "human", 8, "User", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", 10))
    result = reconcile_thread(ledger, thread, [8], [7])
    assert result.status is AdjudicationStatus.NONE
    assert not ledger.context.decisions


def test_ambiguous_publication_is_verified_before_retry(tmp_path: Path) -> None:
    contracts = build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])
    context = new_context(PullRequestBinding(3, "o/r", 4, "a" * 40, "b" * 40, "main"), _thread(), contracts)
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    ledger = store.register(context, "r1")

    class Client:
        calls = 0

        def reply_to_review_thread(self, repository: str, pr: int, root: int, body: str) -> None:
            self.calls += 1
            raise TimeoutError

    client = Client()
    assert publish_context(client, store, ledger, _thread(), *_publication_arguments(ledger)) == "unknown"
    saved = store.publication(context.context_id)[1]
    observed = _thread()
    observed.comments.append(ReviewThreadComment(12, saved))
    assert publish_context(client, store, ledger, observed, *_publication_arguments(ledger)) == "confirmed"
    assert client.calls == 1


def test_publication_definite_refusal_remains_retryable(tmp_path: Path) -> None:
    contracts = build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])
    context = new_context(PullRequestBinding(3, "o/r", 4, "a" * 40, "b" * 40, "main"), _thread(), contracts)
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    ledger = store.register(context, "r1")
    client = MagicMock()
    request = httpx.Request("POST", "https://api.github.test/reply")
    client.reply_to_review_thread.side_effect = httpx.HTTPStatusError("refused", request=request, response=httpx.Response(422, request=request))

    assert publish_context(client, store, ledger, _thread(), *_publication_arguments(ledger)) == "definitely-not-sent"
    assert publish_context(client, store, ledger, _thread(), *_publication_arguments(ledger)) == "definitely-not-sent"
    assert client.reply_to_review_thread.call_count == 2


def test_restart_confirms_interrupted_pending_publication_without_post(tmp_path: Path) -> None:
    contracts = build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])
    context = new_context(PullRequestBinding(3, "o/r", 4, "a" * 40, "b" * 40, "main"), _thread(), contracts)
    path = tmp_path / "state.sqlite"
    first_store = AdjudicationContextStore(path)
    first_store.register(context, "r1")
    result = AdjudicationContextStore(tmp_path / "render.sqlite").register(context, "r1").current(None, None, "authoritative thread reconciled")
    body = render_context_projection(context, (), [8], result, "observation-1")
    first_store.set_publication(context.context_id, "pending", body)
    observed = _thread()
    observed.comments.append(ReviewThreadComment(12, body))
    restarted = AdjudicationContextStore(path)
    client = MagicMock()
    client.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    client.get_pr_review_threads_strict.return_value = [observed]
    service = ReviewAdjudicationService(client, restarted)
    pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}

    snapshots = service.refresh("o/r", 4, pr, [9], [7], [8])
    assert len(snapshots) == 1
    assert restarted.publication(context.context_id)[0] == "confirmed"
    client.reply_to_review_thread.assert_not_called()


def test_engine_targeted_refresh_uses_authoritative_contracts_and_publishes(tmp_path: Path, monkeypatch) -> None:
    """The supported controller boundary, rather than a helper, creates snapshots."""
    from auto_coder.automation_engine import AutomationEngine

    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite"))
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite"))
    monkeypatch.setenv("AUTO_CODER_REVIEW_ADJUDICATION_DB", str(tmp_path / "adjudications.sqlite"))
    github = MagicMock()
    github.get_pull_request_metadata_strict.return_value = {
        "number": 4,
        "body": "Closes #9\nCloses #10",
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}},
    }
    issue_data = {
        9: {"id": 90, "number": 9, "title": "one", "body": BODY},
        10: {"id": 100, "number": 10, "title": "two", "body": BODY.replace("raw value", "second value")},
    }
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda repository, number: issue_data[number]
    thread = _thread()
    github.get_pr_review_threads_strict.return_value = [thread]
    engine = AutomationEngine(github)

    with (
        patch("auto_coder.automation_engine.get_pr_review_allowlist_from_config", return_value=[7]),
        patch("auto_coder.automation_engine.get_review_adjudicator_allowlist_from_config", return_value=[8]),
    ):
        snapshots = engine.refresh_review_adjudications("o/r", {"number": 4, "body": "Closes #9"})

    assert len(snapshots) == 1
    assert snapshots[0].context is not None
    context_id = snapshots[0].context.context_id
    assert snapshots[0].contributing_issues == (9, 10)
    assert engine.get_review_adjudication_snapshots("o/r", 4) == snapshots
    github.reply_to_review_thread.assert_called_once()
    assert "Contributing Issues: #9, #10" in github.reply_to_review_thread.call_args.args[3]
    assert engine.review_adjudications.store.affected_prs("o/r", 10) == (4,)

    # A projection's own webhook leads to another authoritative refresh, but
    # its deterministic publication identity prevents a duplicate POST.
    published_body = github.reply_to_review_thread.call_args.args[3]
    thread.comments.append(
        ReviewThreadComment(
            12,
            published_body,
            "auto-coder",
            99,
            "Bot",
            "2026-01-02T00:00:00Z",
            "2026-01-02T00:00:00Z",
            10,
        )
    )
    with (
        patch("auto_coder.automation_engine.get_pr_review_allowlist_from_config", return_value=[7]),
        patch("auto_coder.automation_engine.get_review_adjudicator_allowlist_from_config", return_value=[8]),
    ):
        repeated = engine.refresh_review_adjudications("o/r", {"number": 4})
    assert repeated[0].context is not None
    assert repeated[0].context.context_id == context_id
    github.reply_to_review_thread.assert_called_once()

    # The durable reverse association makes an edit to the second contract
    # wake the same PR even though its head did not change.
    asyncio.run(engine.invalidate_entity("o/r", "issue", 10, "delivery-1", "issues", "edited"))
    queued = {(candidate.type, candidate.data["number"]) for candidate in engine.queue._queue}
    assert ("issue", 10) in queued
    assert ("pr", 4) in queued
    assert engine.get_review_adjudication_snapshots("o/r", 4)[0].result.status is AdjudicationStatus.SOURCE_UNAVAILABLE


def test_production_service_permanently_retires_revoked_root(tmp_path: Path) -> None:
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    github.get_pr_review_threads_strict.return_value = [_thread()]
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    service = ReviewAdjudicationService(github, store)
    pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}

    original = service.refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert original.context is not None
    original_id = original.context.context_id
    service.mark_unavailable("o/r", 4, "policy refresh")
    service.apply_authorization_policy("o/r", 4, [], [8])
    github.get_issue_dispatch_snapshot_strict.side_effect = PermissionError("403")
    with pytest.raises(PermissionError, match="403"):
        service.refresh("o/r", 4, pr, [9], [], [8])

    # Restart and later reauthorization cannot revive the retired context.
    github.get_issue_dispatch_snapshot_strict.side_effect = None
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    replacement = ReviewAdjudicationService(github, AdjudicationContextStore(tmp_path / "state.sqlite")).refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert replacement.context is not None
    assert replacement.context.context_id != original_id


def test_production_snapshot_keeps_tip_after_ordinary_discussion(tmp_path: Path) -> None:
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    thread = _thread()
    github.get_pr_review_threads_strict.return_value = [thread]
    service = ReviewAdjudicationService(github, AdjudicationContextStore(tmp_path / "state.sqlite"))
    pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}
    initial = service.refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert initial.context is not None
    decision = Decision(
        "00000000-0000-4000-8000-000000000001",
        initial.context.context_id,
        "a" * 40,
        initial.context.contract_digest,
        "UPHOLD",
        "FIX",
        (),
        "The finding remains valid.",
        "dashboard",
    )
    thread.comments.extend(
        [
            ReviewThreadComment(20, render_decision(decision), "judge", 8, "User", "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z", 10),
            ReviewThreadComment(21, "Thanks", "judge", 8, "User", "2026-01-04T00:00:00Z", "2026-01-04T00:00:00Z", 10),
        ]
    )
    current = service.refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert current.result.status is AdjudicationStatus.APPLICABLE
    assert current.result.decision_id == decision.decision_id
    assert current.result.tips == (decision.decision_id,)
    assert github.reply_to_review_thread.call_count == 2
    assert f"Current predecessor tips: {decision.decision_id}" in github.reply_to_review_thread.call_args.args[3]


def test_empty_adjudicator_policy_does_not_issue_context_for_new_root(tmp_path: Path) -> None:
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    first = _thread()
    github.get_pr_review_threads_strict.return_value = [first]
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    service = ReviewAdjudicationService(github, store)
    pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}
    service.refresh("o/r", 4, pr, [9], [7], [8])
    second = ReviewThread(
        id="T2",
        comments=[ReviewThreadComment(30, "second finding", "bot", 7, "Bot", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z")],
    )
    github.get_pr_review_threads_strict.return_value = [first, second]

    service.refresh("o/r", 4, pr, [9], [7], [])

    assert len(store.ledgers_for_pr("o/r", 4)) == 1
    assert github.reply_to_review_thread.call_count == 1


def test_engine_invalid_configuration_suspends_applicable_snapshot(tmp_path: Path, monkeypatch) -> None:
    from auto_coder.automation_engine import AutomationEngine

    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite"))
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite"))
    monkeypatch.setenv("AUTO_CODER_REVIEW_ADJUDICATION_DB", str(tmp_path / "adjudications.sqlite"))
    github = MagicMock()
    github.get_pull_request_metadata_strict.return_value = {
        "number": 4,
        "body": "Closes #9",
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}},
    }
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    thread = _thread()
    github.get_pr_review_threads_strict.return_value = [thread]
    engine = AutomationEngine(github)
    with (
        patch("auto_coder.automation_engine.get_pr_review_allowlist_from_config", return_value=[7]),
        patch("auto_coder.automation_engine.get_review_adjudicator_allowlist_from_config", return_value=[8]),
    ):
        initial = engine.refresh_review_adjudications("o/r", {"number": 4})[0]
        assert initial.context is not None
        decision = Decision(
            "00000000-0000-4000-8000-000000000002",
            initial.context.context_id,
            "a" * 40,
            initial.context.contract_digest,
            "UPHOLD",
            "FIX",
            (),
            "Still valid.",
            "dashboard",
        )
        thread.comments.append(ReviewThreadComment(20, render_decision(decision), "judge", 8, "User", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z", 10))
        assert engine.refresh_review_adjudications("o/r", {"number": 4})[0].result.status is AdjudicationStatus.APPLICABLE

    with (
        patch("auto_coder.automation_engine.get_pr_review_allowlist_from_config", return_value=[7]),
        patch("auto_coder.automation_engine.get_review_adjudicator_allowlist_from_config", side_effect=ValueError("invalid adjudicator list")),
        pytest.raises(ValueError, match="invalid adjudicator list"),
    ):
        engine.refresh_review_adjudications("o/r", {"number": 4})
    assert engine.get_review_adjudication_snapshots("o/r", 4)[0].result.status is AdjudicationStatus.SOURCE_UNAVAILABLE


def test_stale_writer_cannot_resurrect_durably_retired_context(tmp_path: Path) -> None:
    contracts = build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])
    context = new_context(PullRequestBinding(3, "o/r", 4, "a" * 40, "b" * 40, "main"), _thread(), contracts)
    path = tmp_path / "state.sqlite"
    first = AdjudicationContextStore(path)
    first.register(context, "initial")
    stale = first.ledgers_for_pr("o/r", 4)[0]
    second = AdjudicationContextStore(path)
    revoked = second.ledgers_for_pr("o/r", 4)[0]
    revoked.retire("authorization revoked")
    second.save(revoked, "revoked")

    first.save(stale, "stale-writer")

    assert stale.context.retired_reason == "authorization revoked"
    assert AdjudicationContextStore(path).ledgers_for_pr("o/r", 4) == ()


@pytest.mark.parametrize(
    "changed",
    [
        PullRequestBinding(3, "o/r", 4, "c" * 40, "b" * 40, "main"),
        PullRequestBinding(3, "o/r", 4, "a" * 40, "c" * 40, "main"),
        PullRequestBinding(3, "o/r", 4, "a" * 40, "b" * 40, "release"),
    ],
)
def test_observed_revision_change_retires_before_later_read_failure(tmp_path: Path, changed: PullRequestBinding) -> None:
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    github.get_pr_review_threads_strict.return_value = [_thread()]
    path = tmp_path / "state.sqlite"
    service = ReviewAdjudicationService(github, AdjudicationContextStore(path))
    original_pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}
    original = service.refresh("o/r", 4, original_pr, [9], [7], [8])[0]
    assert original.context is not None
    original_id = original.context.context_id
    changed_pr = {
        "head": {"sha": changed.head_sha},
        "base": {"sha": changed.base_sha, "ref": changed.base_ref, "repo": {"id": changed.repository_id}},
    }
    github.get_issue_dispatch_snapshot_strict.side_effect = PermissionError("403")

    with pytest.raises(PermissionError, match="403"):
        service.refresh("o/r", 4, changed_pr, [9], [7], [8])

    github.get_issue_dispatch_snapshot_strict.side_effect = None
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    replacement = ReviewAdjudicationService(github, AdjudicationContextStore(path)).refresh("o/r", 4, original_pr, [9], [7], [8])[0]
    assert replacement.context is not None
    assert replacement.context.context_id != original_id


def test_concurrent_retirement_cannot_leave_applicable_service_snapshot(tmp_path: Path) -> None:
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.return_value = {"id": 90, "number": 9, "title": "one", "body": BODY}
    thread = _thread()
    github.get_pr_review_threads_strict.return_value = [thread]
    path = tmp_path / "state.sqlite"
    store = AdjudicationContextStore(path)
    service = ReviewAdjudicationService(github, store)
    pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}
    initial = service.refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert initial.context is not None
    decision = Decision(
        "00000000-0000-4000-8000-000000000003",
        initial.context.context_id,
        "a" * 40,
        initial.context.contract_digest,
        "UPHOLD",
        "FIX",
        (),
        "Still valid.",
        "dashboard",
    )
    thread.comments.append(ReviewThreadComment(20, render_decision(decision), "judge", 8, "User", "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z", 10))
    assert service.refresh("o/r", 4, pr, [9], [7], [8])[0].result.status is AdjudicationStatus.APPLICABLE
    original_save = store.save
    raced = False

    def save_after_competing_retirement(ledger, observation_revision):
        nonlocal raced
        if not raced:
            raced = True
            competing_store = AdjudicationContextStore(path)
            competing = competing_store.ledgers_for_pr("o/r", 4)[0]
            competing.retire("concurrent authorization revocation")
            competing_store.save(competing, "revoked")
        original_save(ledger, observation_revision)

    store.save = save_after_competing_retirement
    snapshot = service.refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert snapshot.context is not None and snapshot.context.retired_reason is not None
    assert snapshot.result.status is AdjudicationStatus.INVALID
    assert service.snapshots("o/r", 4)[0].result.status is AdjudicationStatus.INVALID


@pytest.mark.parametrize("changed_body", [BODY.replace("Keep π exact.", "Keep p exact."), BODY.replace("raw value", "changed value")])
def test_observed_contract_change_retires_before_thread_failure(tmp_path: Path, changed_body: str) -> None:
    github = MagicMock()
    issue = {"id": 90, "number": 9, "title": "one", "body": BODY}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda repository, number: issue
    github.get_pr_review_threads_strict.return_value = [_thread()]
    path = tmp_path / "state.sqlite"
    service = ReviewAdjudicationService(github, AdjudicationContextStore(path))
    pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 3}}}
    original = service.refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert original.context is not None
    original_id = original.context.context_id
    issue["body"] = changed_body
    github.get_pr_review_threads_strict.side_effect = PermissionError("403")

    with pytest.raises(PermissionError, match="403"):
        service.refresh("o/r", 4, pr, [9], [7], [8])

    issue["body"] = BODY
    github.get_pr_review_threads_strict.side_effect = None
    github.get_pr_review_threads_strict.return_value = [_thread()]
    replacement = ReviewAdjudicationService(github, AdjudicationContextStore(path)).refresh("o/r", 4, pr, [9], [7], [8])[0]
    assert replacement.context is not None
    assert replacement.context.context_id != original_id
