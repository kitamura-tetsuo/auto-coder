from pathlib import Path

from auto_coder.claude_followup_waits import (
    ClaudeFollowupWait,
    ClaudeFollowupWaitStore,
    wait_from_error,
)
from auto_coder.exceptions import (
    ClaudeFollowupDeferralReason,
    ClaudeFollowupUsageLimitError,
    DeliveryCertainty,
)


def _wait(deadline: float, certainty: DeliveryCertainty = DeliveryCertainty.NOT_SENT) -> ClaudeFollowupWait:
    return ClaudeFollowupWait(
        "owner/repo",
        42,
        "claude-a",
        "oauth:fingerprint",
        "task-1",
        "review-thread-repair",
        "finding-1",
        "QUOTA_INSUFFICIENT",
        100.0,
        deadline,
        certainty,
    )


def test_wait_survives_reconstruction_and_never_shortens(tmp_path: Path) -> None:
    path = tmp_path / "waits.sqlite3"
    first = ClaudeFollowupWaitStore(path)
    first.retain(_wait(700.0), ("five_hour",))
    first.retain(_wait(200.0), ("five_hour",))

    recovered = ClaudeFollowupWaitStore(path)
    assert recovered.active_hold("owner/repo", "claude-a", "oauth:fingerprint", now=150.0) == 700.0
    assert recovered.get("owner/repo", "task-1", "review-thread-repair", "finding-1") == _wait(700.0)


def test_different_work_coexists_and_confirmed_unsent_work_retires(tmp_path: Path) -> None:
    store = ClaudeFollowupWaitStore(tmp_path / "waits.sqlite3")
    store.retain(_wait(700.0))
    second = ClaudeFollowupWait(**{**_wait(800.0).__dict__, "work_identity": "finding-2"})
    store.retain(second)
    store.retire("owner/repo", "task-1", "review-thread-repair", "finding-1")

    assert store.get("owner/repo", "task-1", "review-thread-repair", "finding-1") is None
    assert store.get("owner/repo", "task-1", "review-thread-repair", "finding-2") == second


def test_indeterminate_delivery_cannot_be_released_by_deadline(tmp_path: Path) -> None:
    store = ClaudeFollowupWaitStore(tmp_path / "waits.sqlite3")
    store.retain(_wait(120.0, DeliveryCertainty.INDETERMINATE))
    store.retire("owner/repo", "task-1", "review-thread-repair", "finding-1")

    retained = store.get("owner/repo", "task-1", "review-thread-repair", "finding-1")
    assert retained is not None
    assert retained.certainty is DeliveryCertainty.INDETERMINATE
    assert store.active_hold("owner/repo", "claude-a", "oauth:fingerprint", now=900.0) == 120.0


def test_typed_error_is_retained_without_secret_material() -> None:
    error = ClaudeFollowupUsageLimitError(
        ClaudeFollowupDeferralReason.QUOTA_UNAVAILABLE,
        "owner/repo",
        "claude-a",
        "oauth:fingerprint",
        100.0,
        160.0,
        DeliveryCertainty.NOT_SENT,
        diagnostic="endpoint unavailable",
    )
    wait = wait_from_error(error, 42, "task-1", "adversarial-feedback", "generation-1")

    assert wait.reason == "QUOTA_UNAVAILABLE"
    assert wait.retry_not_before == 160.0
    assert "token" not in repr(wait).lower()
