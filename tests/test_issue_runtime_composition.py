from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from auto_coder.github_pending_work import PendingWorkStore, StageOutcome, WorkIdentity
from auto_coder.issue_runtime_composition import IssueRuntimeComposition
from auto_coder.util.github_request_outcome import GitHubApiOutcome


class _Handler:
    def dispatch(self, obligation):
        return StageOutcome(completed_effects=obligation.unfinished_effects)

    recover = dispatch


def test_composition_registers_and_starts_each_executor_once(tmp_path, monkeypatch):
    issue_jobs = MagicMock()
    runner = MagicMock()
    runner.poll.return_value = 0
    issue_jobs.new_runner.return_value = runner
    composition = IssueRuntimeComposition(
        "owner/repo",
        PendingWorkStore(tmp_path / "pending.db"),
        issue_jobs,
        MagicMock(),
    )
    composition.register({"issue-stage": _Handler()})
    scheduled: list[tuple[str, int]] = []
    receipt = MagicMock(repository="owner/repo", request_id="request-1", issue_number=42)
    foreign = MagicMock(repository="other/repo", request_id="request-foreign", issue_number=9)
    monkeypatch.setattr(
        "auto_coder.issue_runtime_composition.RetryDispatchRepository.list_unfinished_accepted",
        lambda _self: [receipt, foreign],
    )

    async def scenario():
        shutdown = asyncio.Event()
        pending_task = composition.start_recovery(
            shutdown,
            schedule_retry=lambda request_id, issue: scheduled.append((request_id, issue)),
        )
        local_runner, local_task = composition.start_local_jobs(
            shutdown,
            capacity=2,
            completion_wake=lambda _job: None,
        )
        await asyncio.sleep(0)
        assert local_runner is runner
        assert scheduled == [("request-1", 42)]
        assert issue_jobs.new_runner.call_count == 1
        with pytest.raises(RuntimeError, match="already active"):
            composition.start_recovery(shutdown, schedule_retry=lambda _request, _issue: None)
        with pytest.raises(RuntimeError, match="already active"):
            composition.start_local_jobs(shutdown, capacity=2, completion_wake=lambda _job: None)
        shutdown.set()
        await asyncio.gather(pending_task, local_task)

    asyncio.run(scenario())


def test_composition_pending_scheduler_keeps_repository_scope(tmp_path):
    store = PendingWorkStore(tmp_path / "pending.db")
    composition = IssueRuntimeComposition("owner/repo", store, MagicMock(), MagicMock())
    composition.register({"issue-stage": _Handler()})
    own = WorkIdentity("owner/repo", "issue:1", "issue-stage")
    foreign = WorkIdentity("other/repo", "issue:2", "issue-stage")
    error = MagicMock()
    error.outcome.classification = GitHubApiOutcome.PRIMARY_THROTTLED
    error.outcome.delivery.value = "http_response_received"
    error.outcome.metadata.retry_after_seconds = 0.0
    error.retry_at = None
    store.defer(own, error, ("effect",))
    store.defer(foreign, error, ("effect",))

    async def scenario():
        await composition.pending_work._dispatch_due()
        await asyncio.gather(*tuple(composition.pending_work._tasks))

    asyncio.run(scenario())
    assert store.get(own) is None
    assert store.get(foreign) is not None
