"""Negative dependency observations, durable wakeup, and worker admission."""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.dependency_observation_cache import DependencyObservationCache
from auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity
from auto_coder.webhook_server import process_github_payload


def issue(number=2019, state="open", body="Parent-Issue: #2016\nBlocked-By: #2018", updated="2026-09-12T10:00:00Z"):
    return {"number": number, "title": "Example", "body": body, "state": state, "updated_at": updated}


def test_trust_is_per_issue_and_restart_discards_it():
    cache = DependencyObservationCache()
    assert cache.get("owner/repo", 2019) is None
    assert cache.observe("owner/repo", issue(), version=0)
    assert cache.observe("owner/repo", issue(2018))
    assert cache.waiting_on("owner/repo", 2019) == (2018,)
    cache.observe("owner/repo", issue(2020))
    assert cache.waiting_on("owner/repo", 2019) == (2018,)
    assert cache.waiting_on("another/repo", 2019) == ()
    assert DependencyObservationCache().waiting_on("owner/repo", 2019) == ()


def test_close_edit_and_reopen_change_negative_evidence():
    cache = DependencyObservationCache()
    cache.observe("r", issue())
    cache.observe("r", issue(2018))
    cache.observe("r", issue(2018, state="closed", updated="2026-09-12T10:01:00Z"))
    assert cache.waiting_on("r", 2019) == ()
    cache.observe("r", issue(2018, updated="2026-09-12T10:02:00Z"))
    assert cache.waiting_on("r", 2019) == (2018,)
    cache.observe("r", issue(body="Parent-Issue: #2016\nBlocked-By:", updated="2026-09-12T10:03:00Z"))
    assert cache.waiting_on("r", 2019) == ()


def test_old_and_equal_timestamp_conflicts_require_refetch():
    cache = DependencyObservationCache()
    cache.observe("r", issue())
    cache.observe("r", issue(state="closed"))
    assert cache.get("r", 2019) is None
    cache.observe("r", issue())
    assert cache.get("r", 2019) is None
    cache.observe("r", issue(), version=cache.version("r", 2019))
    assert cache.get("r", 2019).state == "open"
    cache.observe("r", issue(updated="2026-09-12T09:00:00Z"))
    assert cache.get("r", 2019) is None


def test_webhook_fences_rest_read_in_flight():
    cache = DependencyObservationCache()
    version = cache.version("r", 2019)
    cache.observe("r", issue(state="closed"))
    assert not cache.observe("r", issue(), version=version)
    assert cache.get("r", 2019).state == "closed"


def test_expiry_and_replay_cannot_extend_trust(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("auto_coder.dependency_observation_cache.time.monotonic", lambda: clock[0])
    # The dataclass factory is bound at definition; explicitly control stored time.
    cache = DependencyObservationCache()
    cache.observe("r", issue())
    observed = cache.get("r", 2019)
    clock[0] = observed.observed_at + 301
    cache.observe("r", issue())
    assert cache.get("r", 2019) is None


@pytest.mark.parametrize("body", ["Blocked-By: #2018", "Parent-Issue: #2016\nBlocked-By: nonsense", "Parent-Issue: #2016\nBlocked-By: #2019"])
def test_invalid_or_self_dependency_never_refuses(body):
    cache = DependencyObservationCache()
    cache.observe("r", issue(body=body))
    cache.observe("r", issue(2018))
    assert cache.waiting_on("r", 2019) == ()


def engine_with_claim(tmp_path):
    engine = AutomationEngine(MagicMock(), AutomationConfig(repo_name="owner/repo"))
    engine._route_issue_stages_authoritatively = MagicMock()
    engine.invalidations = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    identity = EntityIdentity("owner/repo", "issue", 2019)
    engine.invalidations.invalidate(identity)
    claim = engine.invalidations.claim("owner/repo")
    assert engine.invalidations.begin_processing(claim)
    return engine, identity, claim


def test_cold_wait_fetches_only_target_and_dependency_and_retains_retry(tmp_path):
    engine, identity, claim = engine_with_claim(tmp_path)
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = lambda repo, number: issue(number)
    assert engine._defer_observed_dependency_wait("owner/repo", 2019, claim)
    assert [call.args for call in engine.github.get_issue_dispatch_snapshot_strict.call_args_list] == [("owner/repo", 2019), ("owner/repo", 2018), ("owner/repo", 2019)]
    engine._route_issue_stages_authoritatively.assert_called_once()
    deferred = engine.invalidations.get_deferred(identity)
    assert deferred.reason == "cached_dependency_wait"
    assert engine.invalidations.claim("owner/repo") is None
    assert engine.implementation_slots is None


def test_warm_wait_authoritatively_routes_and_close_webhook_wakes_it(tmp_path):
    engine, identity, claim = engine_with_claim(tmp_path)
    engine.dependency_observations.observe("owner/repo", issue())
    engine.dependency_observations.observe("owner/repo", issue(2018))
    engine.github.get_issue_dispatch_snapshot_strict.return_value = issue()
    assert engine._defer_observed_dependency_wait("owner/repo", 2019, claim)
    payload = {"repository": {"full_name": "owner/repo"}, "action": "closed", "issue": issue(2018, state="closed", updated="2026-09-12T10:01:00Z")}
    asyncio.run(process_github_payload("issues", payload, engine, "owner/repo", "close-2018"))
    assert engine.dependency_observations.waiting_on("owner/repo", 2019) == ()
    assert engine.invalidations.get_deferred(identity) is None
    engine.github.get_issue_dispatch_snapshot_strict.assert_called_once_with("owner/repo", 2019)
    engine._route_issue_stages_authoritatively.assert_called_once()


def test_worker_routes_before_dependency_deferral_without_candidate_refresh(tmp_path):
    engine, identity, claim = engine_with_claim(tmp_path)
    engine.invalidations.release(claim)
    engine.dependency_observations.observe("owner/repo", issue())
    engine.dependency_observations.observe("owner/repo", issue(2018))
    engine._cached_issue_refusal = MagicMock(return_value=None)
    engine._create_candidate_from_single = MagicMock(side_effect=AssertionError("unexpected refresh"))
    engine._validate_submitted_parent_generation_for_child = MagicMock(side_effect=AssertionError("unexpected validation"))
    engine.github.get_issue_dispatch_snapshot_strict.return_value = issue()

    async def scenario():
        await engine._enqueue_pending_invalidations("owner/repo")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            await asyncio.wait_for(engine.queue.join(), timeout=5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    assert engine.invalidations.get_deferred(identity).reason == "cached_dependency_wait"
    engine._create_candidate_from_single.assert_not_called()
    engine._validate_submitted_parent_generation_for_child.assert_not_called()
    engine.github.get_issue_dispatch_snapshot_strict.assert_called_once_with("owner/repo", 2019)
    engine._route_issue_stages_authoritatively.assert_called_once()
    assert engine.implementation_slots is None
    assert engine.active_workers[0] is None


def test_dependency_wakeup_preserves_rate_limit_guard(tmp_path):
    engine, identity, claim = engine_with_claim(tmp_path)
    engine.invalidations.defer(claim, "rate_limit_cooldown", 9999999999)
    engine.invalidations.wake_dependency_waiters("owner/repo")
    assert engine.invalidations.get_deferred(identity).retry_not_before == 9999999999
    assert engine.invalidations.claim("owner/repo") is None


def test_closed_dependency_requires_normal_admission(tmp_path):
    engine, identity, claim = engine_with_claim(tmp_path)
    engine.dependency_observations.observe("owner/repo", issue())
    engine.dependency_observations.observe("owner/repo", issue(2018, state="closed"))
    assert engine._defer_observed_dependency_wait("owner/repo", 2019, claim) is False
    assert engine.invalidations.get_deferred(identity) is None
    assert engine.github.mock_calls == []


def test_malformed_webhook_invalidates_only_affected_issue():
    cache = DependencyObservationCache()
    cache.observe("r", issue())
    cache.observe("r", issue(2018))
    assert cache.observe("r", {"number": 2018, "state": "closed"}) is False
    assert cache.get("r", 2018) is None
    assert cache.get("r", 2019) is not None
    assert cache.waiting_on("r", 2019) == ()


def test_close_between_observation_and_persist_does_not_hide_wakeup(tmp_path, monkeypatch):
    engine, identity, claim = engine_with_claim(tmp_path)
    cache = engine.dependency_observations
    cache.observe("owner/repo", issue())
    cache.observe("owner/repo", issue(2018))
    engine.github.get_issue_dispatch_snapshot_strict.return_value = issue()
    defer = engine.invalidations.defer

    def close_then_defer(*args):
        cache.observe("owner/repo", issue(2018, state="closed", updated="2026-09-12T10:01:00Z"))
        engine.invalidations.wake_dependency_waiters("owner/repo")
        return defer(*args)

    monkeypatch.setattr(engine.invalidations, "defer", close_then_defer)
    assert engine._defer_observed_dependency_wait("owner/repo", 2019, claim)
    assert engine.invalidations.get_deferred(identity) is None
    assert engine.invalidations.claim("owner/repo") is not None


@pytest.mark.parametrize("number", [2019, 2018])
def test_missing_issue_falls_through_to_authoritative_classification(tmp_path, number):
    engine, identity, claim = engine_with_claim(tmp_path)
    if number == 2018:
        engine.dependency_observations.observe("owner/repo", issue())
    response = httpx.Response(404, request=httpx.Request("GET", f"https://api.github.com/repos/owner/repo/issues/{number}"))
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = httpx.HTTPStatusError("Not found", request=response.request, response=response)
    assert engine._defer_observed_dependency_wait("owner/repo", 2019, claim) is False
    assert engine.invalidations.get_deferred(identity) is None
