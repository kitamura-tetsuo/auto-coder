from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_coder.issue_stage_routing import ImplementationRetryRequest
from auto_coder.retry_dispatch import RetryDispatchConflict, RetryDispatchRepository


def authority(request: str = "request-1", attempt: str = "attempt-1") -> ImplementationRetryRequest:
    return ImplementationRetryRequest(
        request_id=request,
        repository="owner/repo",
        target_number=2185,
        generation="generation-1",
        attempt_id=attempt,
        status="owned",
        ownership_reference="execution-1",
    )


def test_claim_is_durable_and_suppresses_replay_and_restart(tmp_path):
    path = tmp_path / "handoffs.db"
    first = RetryDispatchRepository("owner/repo", path)
    claimed, may_create = first.claim(authority(), "codex-cloud", "codex-alias", {"base_branch": "main"})

    assert may_create is True
    assert claimed.outcome == "claimed"
    assert claimed.suppresses_creation is True

    replay = RetryDispatchRepository("owner/repo", path)
    recovered, may_create_again = replay.claim(authority(), "codex-cloud", "codex-alias", {"base_branch": "main"})
    assert may_create_again is False
    assert recovered.creation_id == claimed.creation_id
    assert recovered.outcome == "claimed"


def test_only_definitely_not_started_reopens_same_creation(tmp_path):
    store = RetryDispatchRepository("owner/repo", tmp_path / "handoffs.db")
    claimed, _ = store.claim(authority(), "local", "codex", {"workspace": "existing"})
    store.record_outcome("request-1", "definitely-not-started", diagnostic="exec was rejected before spawn")

    retried, may_create = store.claim(authority(), "local", "codex", {"workspace": "existing"})
    assert may_create is True
    assert retried.creation_id == claimed.creation_id
    assert retried.outcome == "claimed"

    store.record_outcome("request-1", "indeterminate", diagnostic="process response was lost")
    uncertain, may_create_uncertain = store.claim(authority(), "local", "codex", {"workspace": "existing"})
    assert may_create_uncertain is False
    assert uncertain.outcome == "indeterminate"


def test_definitely_not_started_allows_configured_fallback_with_same_attempt(tmp_path):
    store = RetryDispatchRepository("owner/repo", tmp_path / "handoffs.db")
    first, _ = store.claim(authority(), "claude-routine", "routine-alias", {"base_branch": "main"})
    store.record_outcome("request-1", "definitely-not-started", diagnostic="quota rejected before submission")

    fallback, may_create = store.claim(authority(), "codex-cloud", "codex-alias", {"base_branch": "main"})

    assert may_create is True
    assert fallback.creation_id == first.creation_id
    assert fallback.attempt_id == first.attempt_id
    assert (fallback.route, fallback.backend_name, fallback.outcome) == ("codex-cloud", "codex-alias", "claimed")


def test_numeric_attempt_is_allocated_once_above_all_retained_evidence(tmp_path):
    store = RetryDispatchRepository("owner/repo", tmp_path / "handoffs.db")
    store.claim(authority(), "codex-cloud", "codex", {"base_branch": "main"})
    first = store.allocate_numeric_attempt("request-1", [2, 7, 4])
    replay = store.allocate_numeric_attempt("request-1", [100])

    assert first.numeric_attempt == 8
    assert replay.numeric_attempt == 8

    second_authority = authority("request-2", "attempt-2")
    store.claim(second_authority, "codex-cloud", "codex", {"base_branch": "main"})
    second = store.allocate_numeric_attempt("request-2", [3])
    assert second.numeric_attempt == 9


def test_contention_allows_only_one_external_creation_owner(tmp_path):
    path = tmp_path / "handoffs.db"

    def contend() -> tuple[str, bool]:
        handoff, won = RetryDispatchRepository("owner/repo", path).claim(authority(), "claude-routine", "routine-alias", {"base_branch": "main"})
        return handoff.creation_id, won

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: contend(), range(2)))

    assert sum(won for _, won in results) == 1
    assert len({creation_id for creation_id, _ in results}) == 1


def test_accepted_receipt_is_immutable_and_tracking_is_separate(tmp_path):
    store = RetryDispatchRepository("owner/repo", tmp_path / "handoffs.db")
    store.claim(authority(), "jules", "jules-alias", {"base_branch": "main"})
    accepted = store.record_outcome("request-1", "accepted", external_id="session-7", external_url="https://provider/tasks/7")
    assert accepted.tracking_complete is False
    assert accepted.external_id == "session-7"

    tracked = store.mark_tracking_complete("request-1")
    assert tracked.tracking_complete is True
    with pytest.raises(RetryDispatchConflict, match="immutable"):
        store.record_outcome("request-1", "accepted", external_id="session-8")
    with pytest.raises(RetryDispatchConflict, match="cannot become unsent"):
        store.record_outcome("request-1", "definitely-not-started", external_id="session-7")


@pytest.mark.parametrize("status", ["pending", "invalidated"])
def test_unowned_or_invalidated_authority_fails_closed(tmp_path, status):
    unowned = ImplementationRetryRequest(
        request_id="request-1",
        repository="owner/repo",
        target_number=2185,
        generation="generation-1",
        attempt_id="attempt-1",
        status=status,
    )
    store = RetryDispatchRepository("owner/repo", tmp_path / "handoffs.db")
    with pytest.raises(RetryDispatchConflict, match="has not acquired"):
        store.claim(unowned, "local", "codex", {})
    assert store.list_for_issue(2185) == ()


def test_route_identity_cannot_be_reinterpreted_and_credentials_are_rejected(tmp_path):
    store = RetryDispatchRepository("owner/repo", tmp_path / "handoffs.db")
    store.claim(authority(), "codex-cloud", "named-a", {"base_branch": "main"})
    with pytest.raises(RetryDispatchConflict, match="different dispatch inputs"):
        store.claim(authority(), "codex-cloud", "named-b", {"base_branch": "main"})

    other = authority("request-2", "attempt-2")
    with pytest.raises(ValueError, match="must not persist credentials"):
        store.claim(other, "codex-cloud", "named-a", {"api_token": "do-not-write"})
    assert b"do-not-write" not in (tmp_path / "handoffs.db").read_bytes()


def test_machine_readable_diagnostics_preserve_full_identity(tmp_path):
    store = RetryDispatchRepository("owner/repo", tmp_path / "handoffs.db")
    store.claim(authority(), "local", "codex", {"workspace": "retained"})
    store.record_outcome("request-1", "completed", external_id="invocation-4", diagnostic="result checkpoint retained")

    [record] = store.list_for_issue(2185)
    assert (
        record.repository,
        record.issue_number,
        record.request_id,
        record.attempt_id,
        record.generation,
        record.route,
        record.backend_name,
        record.outcome,
        record.external_id,
        record.diagnostic,
    ) == (
        "owner/repo",
        2185,
        "request-1",
        "attempt-1",
        "generation-1",
        "local",
        "codex",
        "completed",
        "invocation-4",
        "result checkpoint retained",
    )
