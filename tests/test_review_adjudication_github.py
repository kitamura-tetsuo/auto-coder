import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.review_adjudication import AdjudicationStatus
from auto_coder.review_adjudication_github import AdjudicationContextStore, IssueEvidence, PullRequestBinding, build_issue_contracts, new_context, publish_context, reconcile_thread
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

BODY = """## Objective

Keep π exact.

## Requirements

REQ-001: Preserve the raw value.
"""


def _thread() -> ReviewThread:
    return ReviewThread(id="T1", comments=[ReviewThreadComment(10, "finding", "bot", 7, "Bot", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")])


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
    assert result.status is AdjudicationStatus.INVALID
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
    assert publish_context(client, store, ledger, _thread()) == "unknown"
    saved = store.publication(context.context_id)[1]
    observed = _thread()
    observed.comments.append(ReviewThreadComment(12, saved))
    assert publish_context(client, store, ledger, observed) == "confirmed"
    assert client.calls == 1


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
    github.get_issue_dispatch_snapshot_strict.side_effect = [
        {"id": 90, "number": 9, "title": "one", "body": BODY},
        {"id": 100, "number": 10, "title": "two", "body": BODY.replace("raw value", "second value")},
    ]
    github.get_pr_review_threads_strict.return_value = [_thread()]
    engine = AutomationEngine(github)

    with (
        patch("auto_coder.automation_engine.get_pr_review_allowlist_from_config", return_value=[7]),
        patch("auto_coder.automation_engine.get_review_adjudicator_allowlist_from_config", return_value=[8]),
    ):
        snapshots = engine.refresh_review_adjudications("o/r", {"number": 4, "body": "Closes #9\nCloses #10"})

    assert len(snapshots) == 1
    assert snapshots[0].contributing_issues == (9, 10)
    assert engine.get_review_adjudication_snapshots("o/r", 4) == snapshots
    github.reply_to_review_thread.assert_called_once()
    assert "Contributing Issues: #9, #10" in github.reply_to_review_thread.call_args.args[3]
    assert engine.review_adjudications.store.affected_prs("o/r", 10) == (4,)

    # The durable reverse association makes an edit to the second contract
    # wake the same PR even though its head did not change.
    asyncio.run(engine.invalidate_entity("o/r", "issue", 10, "delivery-1", "issues", "edited"))
    queued = {(candidate.type, candidate.data["number"]) for candidate in engine.queue._queue}
    assert ("issue", 10) in queued
    assert ("pr", 4) in queued
