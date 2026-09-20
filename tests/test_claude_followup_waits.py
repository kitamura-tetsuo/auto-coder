import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.automation_config import CandidateProcessingResult, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.claude_followup_waits import (
    ClaudeFollowupHoldActive,
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


def test_context_hold_blocks_different_work_before_usage_read(tmp_path: Path) -> None:
    store = ClaudeFollowupWaitStore(tmp_path / "waits.sqlite3")
    store.retain(_wait(700.0))

    with pytest.raises(ClaudeFollowupHoldActive) as raised:
        with store.admission("owner/repo", "claude-a", "oauth:fingerprint", now=200.0):
            pytest.fail("held context was admitted")

    assert raised.value.retry_not_before == 700.0


def test_newer_refusal_fences_stale_positive_and_due_rechecks_contend(tmp_path: Path) -> None:
    store = ClaudeFollowupWaitStore(tmp_path / "waits.sqlite3")
    store.retain(_wait(120.0))

    with store.admission("owner/repo", "claude-a", "oauth:fingerprint", now=121.0) as first:
        with pytest.raises(ClaudeFollowupHoldActive):
            with store.admission("owner/repo", "claude-a", "oauth:fingerprint", now=121.0):
                pytest.fail("two due rechecks owned the same context")
        newer = ClaudeFollowupWait(**{**_wait(300.0).__dict__, "observed_at": 200.0})
        store.retain(newer)
        assert store.authorize_assignment(first) is False


def test_due_prs_only_include_definitely_unsent_work(tmp_path: Path) -> None:
    store = ClaudeFollowupWaitStore(tmp_path / "waits.sqlite3")
    store.retain(_wait(120.0))
    uncertain = ClaudeFollowupWait(
        **{
            **_wait(100.0, DeliveryCertainty.INDETERMINATE).__dict__,
            "pr_number": 43,
            "work_identity": "finding-uncertain",
        }
    )
    store.retain(uncertain)

    assert store.due_prs("owner/repo", now=121.0) == (42,)


@pytest.mark.asyncio
async def test_daemon_revisits_due_wait_without_external_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ClaudeFollowupWaitStore(tmp_path / "waits.sqlite3")
    store.retain(_wait(0.0))
    shutdown = asyncio.Event()
    processed: list[int] = []

    class GitHub:
        def get_pull_request_metadata_strict(self, repository: str, number: int):
            assert repository == "owner/repo"
            return {"number": number, "state": "open", "head": {"sha": "fresh"}}

        def get_pr_details(self, raw):
            return raw

    async def run_local(_label, function, *args):
        result = function(*args)
        processed.append(args[-1].data["number"])
        shutdown.set()
        return result

    fake = SimpleNamespace(
        _shutdown_event=shutdown,
        github=GitHub(),
        _run_local_critical=run_local,
        _process_single_candidate=lambda *_args, **_kwargs: CandidateProcessingResult(type="pr", number=42, success=True, outcome=PRProcessingOutcome.SUCCESS),
    )
    monkeypatch.setattr("auto_coder.automation_engine.get_claude_followup_wait_store", lambda: store)

    await AutomationEngine._claude_followup_recovery_loop(fake, "owner/repo")

    assert processed == [42]
    assert store.due_prs("owner/repo") == ()
