from pathlib import Path

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
