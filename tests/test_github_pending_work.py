from __future__ import annotations

from auto_coder.github_pending_work import (
    MAX_THROTTLED_RETRIES,
    PendingReason,
    PendingWorkStore,
    WorkIdentity,
)
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubRequestRefused,
    GitHubResponseMetadata,
    RequestProvenance,
)


def _error(classification, *, delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED, retry_after=5):
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("operation", "attempt", "test", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}"),
        None if classification is GitHubApiOutcome.REFUSED else 403,
        classification,
        RequestProvenance.NETWORK,
        delivery,
        GitHubResponseMetadata(retry_after_seconds=retry_after),
        1,
    )
    return GitHubRequestRefused(outcome) if classification is GitHubApiOutcome.REFUSED else GitHubRequestError(outcome)


def test_obligation_survives_restart_and_effects_complete_independently(tmp_path):
    path = tmp_path / "pending.db"
    identity = WorkIdentity("acme/widgets", "issue:12", "validation", "submission-a")
    store = PendingWorkStore(path)
    saved = store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED), ("diagnostic", "readiness-withdrawal"), now=100)

    restarted = PendingWorkStore(path)
    assert restarted.due(now=104) == []
    assert restarted.due(now=105) == [saved]
    assert restarted.complete_effect(identity, "diagnostic") is True
    assert restarted.due(now=105)[0].unfinished_effects == ("readiness-withdrawal",)
    assert restarted.complete_effect(identity, "readiness-withdrawal") is True
    assert restarted.due(now=105) == []


def test_admission_deferral_does_not_spend_throttle_attempt(tmp_path):
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "pr:4", "ci", "head")
    for _ in range(8):
        result = store.defer(identity, _error(GitHubApiOutcome.REFUSED, retry_after=0), ("ci",), governor_deadline=200, now=100)
    assert result.reason is PendingReason.ADMISSION_DEFERRED
    assert result.throttle_attempts == 0
    assert result.not_before == 200


def test_retry_bound_is_semantic_and_survives_restart(tmp_path):
    path = tmp_path / "pending.db"
    identity = WorkIdentity("acme/widgets", "startup", "enumeration", "config-v1")
    for attempt in range(MAX_THROTTLED_RETRIES + 1):
        result = PendingWorkStore(path).defer(
            identity,
            _error(GitHubApiOutcome.PRIMARY_THROTTLED),
            ("complete-authoritative-scan",),
            now=100 + attempt,
        )
    assert result.throttle_attempts == 4
    assert result.reason is PendingReason.RETRIES_EXHAUSTED
    assert result.automatically_retryable is False
    assert PendingWorkStore(path).due(now=1000) == []


def test_auth_and_indeterminate_delivery_are_operational_blocks(tmp_path):
    store = PendingWorkStore(tmp_path / "pending.db")
    auth = store.defer(WorkIdentity("a/b", "issue:1", "read"), _error(GitHubApiOutcome.AUTHENTICATION_FAILURE), ("read",), now=1)
    ambiguous = store.defer(
        WorkIdentity("a/b", "issue:2", "publish"),
        _error(GitHubApiOutcome.TRANSPORT_FAILURE, delivery=DeliveryCertainty.INDETERMINATE),
        ("reconcile-publication",),
        now=1,
    )
    assert auth.reason is PendingReason.AUTHENTICATION
    assert ambiguous.reason is PendingReason.INDETERMINATE
    assert store.due(now=10_000) == []
