"""Regression coverage for provider-neutral durable Issue dispatch admission."""

from __future__ import annotations

import threading
from pathlib import Path

from auto_coder.cloud_manager import CloudManager
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.issue_dispatch import (
    AdapterOutcome,
    CandidateHandoff,
    DispatchOutcome,
    IssueAttemptIdentity,
    IssueDispatchGuard,
)


def _identity(repository: str = "owner/repo", attempt: str = "attempt-one") -> IssueAttemptIdentity:
    owner, name = repository.split("/", 1)
    return IssueAttemptIdentity(owner, name, 2077, attempt)


def _guard(tmp_path: Path, repository: str = "owner/repo") -> IssueDispatchGuard:
    safe_name = repository.replace("/", "-")
    return IssueDispatchGuard(
        tmp_path / f"{safe_name}.sqlite3",
        cloud_run_repository_factory=lambda repo: CloudRunRepository(repo, tmp_path / f"{repo.replace('/', '-')}-runs.json"),
        cloud_manager_factory=lambda repo: CloudManager(repo, tmp_path / f"{repo.replace('/', '-')}-cloud.csv"),
    )


def test_contested_remote_claim_blocks_remote_local_and_restart(tmp_path):
    """AC-001: the durable reservation is visible during the external call."""
    identity = _identity()
    first_guard = _guard(tmp_path)
    callback_entered = threading.Event()
    allow_callback_return = threading.Event()
    callback_count = 0

    def submit() -> AdapterOutcome:
        nonlocal callback_count
        callback_count += 1
        callback_entered.set()
        assert allow_callback_return.wait(timeout=5)
        return AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "provider-task-1")

    holder: list[object] = []
    thread = threading.Thread(target=lambda: holder.append(first_guard.dispatch_remote(identity, CandidateHandoff("cloud-a", "provider-a"), submit)))
    thread.start()
    assert callback_entered.wait(timeout=5)

    second_guard = _guard(tmp_path)
    competing = second_guard.dispatch_remote(
        identity,
        CandidateHandoff("cloud-b", "provider-b"),
        lambda: (_ for _ in ()).throw(AssertionError("second remote callback must not run")),
    )
    local = second_guard.reserve(identity, CandidateHandoff("local", "local"))
    restarted = _guard(tmp_path).inspect(identity)
    assert competing.admitted is False
    assert local.admitted is False
    assert restarted is not None
    assert restarted.outcome == DispatchOutcome.INDETERMINATE
    assert callback_count == 1

    allow_callback_return.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert holder[0].outcome == DispatchOutcome.REMOTE_ACCEPTED


def test_confirmed_not_started_releases_with_new_incarnation_and_stale_result_is_ignored(tmp_path):
    """AC-002: only a durable negative outcome releases, and old authority stays stale."""
    guard = _guard(tmp_path)
    identity = _identity()
    old_claim = guard.reserve(identity, CandidateHandoff("cloud-a", "provider-a"))
    released = guard.finalize(old_claim, AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic="provider confirmed no task"))
    assert released.outcome == DispatchOutcome.NOT_STARTED
    assert guard.inspect(identity) is None

    new_claim = guard.reserve(identity, CandidateHandoff("cloud-b", "provider-b"))
    assert new_claim.admitted is True
    assert new_claim.claim_incarnation != old_claim.claim_incarnation
    stale = guard.finalize(old_claim, AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "late-task"))
    assert stale.outcome == DispatchOutcome.DEFERRED
    persisted = _guard(tmp_path).inspect(identity)
    assert persisted is not None
    assert persisted.claim_incarnation == new_claim.claim_incarnation
    assert persisted.provider_reference == ""


def test_accepted_secondary_failure_is_durable_and_suppresses_every_candidate(tmp_path):
    """AC-003: tracking failure does not erase accepted provider ownership."""
    identity = _identity()
    result = _guard(tmp_path).dispatch_remote(
        identity,
        CandidateHandoff("named-backend", "provider-a"),
        lambda: AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "task-accepted"),
        publish_tracking=lambda _result: False,
    )
    assert result.outcome == DispatchOutcome.REMOTE_ACCEPTED
    assert result.tracking_complete is False
    assert result.provider_reference == "task-accepted"

    restarted = _guard(tmp_path)
    persisted = restarted.inspect(identity)
    assert persisted is not None
    assert persisted.outcome == DispatchOutcome.REMOTE_ACCEPTED
    assert persisted.tracking_complete is False
    assert persisted.backend_name == "named-backend"
    assert persisted.provider == "provider-a"
    assert restarted.reserve(identity, CandidateHandoff("local", "local")).admitted is False


def test_crash_empty_observation_and_configuration_change_preserve_uncertainty(tmp_path):
    """AC-004: a reservation without finalization never manufactures rejection."""
    identity = _identity()
    claim = _guard(tmp_path).reserve(identity, CandidateHandoff("cloud-old", "provider-a"))
    assert claim.admitted is True

    first_restart = _guard(tmp_path).inspect(identity)
    second_restart = _guard(tmp_path).inspect(identity)
    local = _guard(tmp_path).reserve(identity, CandidateHandoff("preferred-local", "local"))
    assert first_restart is not None and first_restart.outcome == DispatchOutcome.INDETERMINATE
    assert second_restart is not None and second_restart.provider_reference == ""
    assert local.admitted is False
    assert local.backend_name == "cloud-old"


def test_storage_failures_fail_closed_before_submission_and_after_observation(tmp_path, monkeypatch):
    """AC-005: callback counts and actual persisted state prove fail-closed behavior."""
    broken_guard = IssueDispatchGuard(tmp_path)  # a directory cannot be opened as SQLite
    calls = 0

    def should_not_run() -> AdapterOutcome:
        nonlocal calls
        calls += 1
        return AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "impossible")

    before = broken_guard.dispatch_remote(_identity(), CandidateHandoff("cloud", "provider"), should_not_run)
    assert before.outcome == DispatchOutcome.DEFERRED
    assert calls == 0

    guard = _guard(tmp_path)
    identity = _identity(attempt="post-call")
    claim = guard.reserve(identity, CandidateHandoff("cloud", "provider"))
    monkeypatch.setattr(guard, "_connect", lambda: (_ for _ in ()).throw(OSError("disk unavailable")))
    after = guard.finalize(claim, AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "possibly-created"))
    assert after.outcome == DispatchOutcome.DEFERRED
    persisted = _guard(tmp_path).inspect(identity)
    assert persisted is not None
    assert persisted.outcome == DispatchOutcome.INDETERMINATE
    assert _guard(tmp_path).reserve(identity, CandidateHandoff("other", "other")).admitted is False


def test_failed_confirmed_release_remains_durably_suppressing(tmp_path, monkeypatch):
    """A failed NOT_STARTED release never authorizes a fallback callback."""
    identity = _identity(attempt="failed-release")
    guard = _guard(tmp_path)
    claim = guard.reserve(identity, CandidateHandoff("cloud", "provider"))
    assert claim.admitted is True
    assert (tmp_path / "owner-repo.sqlite3").is_file()

    monkeypatch.setattr(guard, "_connect", lambda: (_ for _ in ()).throw(OSError("release write unavailable")))
    release = guard.finalize(claim, AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic="provider confirmed absent"))
    assert release.outcome == DispatchOutcome.DEFERRED
    assert release.tracking_complete is False
    assert "ownership finalization failed" in release.diagnostic

    fallback_calls = 0

    def fallback() -> AdapterOutcome:
        nonlocal fallback_calls
        fallback_calls += 1
        return AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "replacement-task")

    restarted = _guard(tmp_path)
    persisted = restarted.inspect(identity)
    fallback_result = restarted.dispatch_remote(identity, CandidateHandoff("fallback", "other-provider"), fallback)
    assert persisted is not None
    assert persisted.outcome == DispatchOutcome.INDETERMINATE
    assert persisted.claim_incarnation == claim.claim_incarnation
    assert fallback_result.admitted is False
    assert fallback_result.outcome == DispatchOutcome.INDETERMINATE
    assert fallback_calls == 0


def test_legacy_production_writers_suppress_conflict_and_preserve_attempt_isolation(tmp_path):
    """AC-006: CloudRun/cloud.csv evidence migrates without invented ownership."""
    run_store = CloudRunRepository("owner/repo", tmp_path / "owner-repo-runs.json")
    manager = CloudManager("owner/repo", tmp_path / "owner-repo-cloud.csv")
    assert run_store.save(CloudRun("owner/repo", 2077, 7, "provider-a", "task-a", "backend-a"))
    assert manager.add_session(2077, "task-a", "provider-a", "backend-a")

    guard = _guard(tmp_path)
    legacy = guard.inspect(_identity(attempt="7"))
    assert legacy is not None
    assert legacy.outcome == DispatchOutcome.REMOTE_ACCEPTED
    assert legacy.provider_reference == "task-a"

    new_attempt = guard.reserve(_identity(attempt="caller-authorized-new"), CandidateHandoff("new", "provider-b"))
    assert new_attempt.outcome == DispatchOutcome.DEFERRED
    assert "no unambiguous attempt association" in new_attempt.diagnostic
    authorized = guard.reserve(
        _identity(attempt="explicitly-authorized"),
        CandidateHandoff("new", "provider-b"),
        authorize_new_attempt=True,
    )
    assert authorized.admitted is True
    retained_binding = guard.get_legacy_issue_ownership("owner", "repo", 2077)
    assert retained_binding is not None
    assert retained_binding.provider_reference == "task-a"
    assert guard.inspect(_identity(attempt="7")).provider_reference == "task-a"

    other_repository = _guard(tmp_path, "other/repo")
    independent = other_repository.reserve(_identity("other/repo", "7"), CandidateHandoff("new", "provider-b"))
    assert independent.admitted is True


def test_legacy_pending_and_contradictory_binding_remain_suppressing(tmp_path):
    run_store = CloudRunRepository("owner/repo", tmp_path / "owner-repo-runs.json")
    manager = CloudManager("owner/repo", tmp_path / "owner-repo-cloud.csv")
    assert run_store.save(CloudRun("owner/repo", 2077, 3, "provider-a", "", "backend-a", submission_outcome="indeterminate"))
    pending = _guard(tmp_path).inspect(_identity(attempt="3"))
    assert pending is not None and pending.outcome == DispatchOutcome.INDETERMINATE

    assert manager.add_session(2077, "different-task", "provider-a", "backend-a")
    assert run_store.save(CloudRun("owner/repo", 2077, 4, "provider-a", "original-task", "backend-a"))
    conflict = _guard(tmp_path).inspect(_identity(attempt="4"))
    assert conflict is not None
    assert conflict.outcome == DispatchOutcome.DEFERRED
    assert "contradict" in conflict.diagnostic


def test_reacquired_legacy_claim_has_new_incarnation_and_rejects_predecessor(tmp_path):
    """A released legacy claim never shares authority with its successor."""
    run_store = CloudRunRepository("owner/repo", tmp_path / "owner-repo-runs.json")
    assert run_store.save(CloudRun("owner/repo", 2077, 5, "provider-a", "", "backend-a", submission_outcome="indeterminate"))
    identity = _identity(attempt="5")
    guard = _guard(tmp_path)

    predecessor = guard.inspect(identity)
    assert predecessor is not None
    assert predecessor.claim_incarnation.startswith("legacy-")
    released = guard.finalize(predecessor, AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic="confirmed absent"))
    assert released.outcome == DispatchOutcome.NOT_STARTED

    successor = guard.inspect(identity)
    assert successor is not None
    assert successor.claim_incarnation.startswith("legacy-")
    assert successor.claim_incarnation != predecessor.claim_incarnation

    late = guard.finalize(predecessor, AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "late-task"))
    assert late.outcome == DispatchOutcome.DEFERRED
    persisted = guard.inspect(identity)
    assert persisted is not None
    assert persisted.claim_incarnation == successor.claim_incarnation
    assert persisted.provider_reference == ""


def test_ranked_candidates_cross_modes_and_advance_only_when_not_started(tmp_path):
    guard = _guard(tmp_path)
    identity = _identity()
    invoked = []

    def invoke(candidate):
        invoked.append(candidate.backend_name)
        if candidate.provider == "codex-cloud":
            return AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic="quota rejected before send")
        return AdapterOutcome(DispatchOutcome.LOCAL_COMPLETED)

    result = guard.dispatch_candidates(
        identity,
        [CandidateHandoff("remote-alias", "codex-cloud"), CandidateHandoff("local-alias", "codex")],
        invoke,
    )

    assert invoked == ["remote-alias", "local-alias"]
    assert result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert (result.backend_name, result.provider) == ("local-alias", "codex")


def test_ranked_candidates_stop_on_indeterminate_and_suppress_restart(tmp_path):
    guard = _guard(tmp_path)
    identity = _identity()
    invoked = []

    def invoke(candidate):
        invoked.append(candidate.backend_name)
        return AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic="response lost")

    result = guard.dispatch_candidates(
        identity,
        [CandidateHandoff("remote", "claude-routine"), CandidateHandoff("local", "codex")],
        invoke,
    )
    restarted = _guard(tmp_path).dispatch_candidates(
        identity,
        [CandidateHandoff("local", "codex")],
        lambda candidate: AdapterOutcome(DispatchOutcome.LOCAL_COMPLETED),
    )

    assert invoked == ["remote"]
    assert result.outcome is DispatchOutcome.INDETERMINATE
    assert restarted.outcome is DispatchOutcome.INDETERMINATE
    assert restarted.backend_name == "remote"


def test_ranked_candidates_deduplicate_and_report_exhaustion(tmp_path):
    invoked = []
    candidate = CandidateHandoff("unavailable", "codex")

    def invoke(item):
        invoked.append((item.backend_name, item.provider))
        return AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic="missing executable")

    result = _guard(tmp_path).dispatch_candidates(_identity(), [candidate, candidate], invoke)

    assert invoked == [("unavailable", "codex")]
    assert result.outcome is DispatchOutcome.DEFERRED
    assert result.backend_name == ""
    assert result.diagnostic == "all ranked candidates were confirmed not started"
