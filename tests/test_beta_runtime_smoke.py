"""Local smoke contracts over real SQLite and production source-evidence readers.

The explicit worker boundary below is a unit-test double, not the production
worker oracle. The independently installed Supervisor/UDS integration suite
owns the genuine heartbeat and installed-lock acceptance evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import pytest

from auto_coder.beta_source_selection import MutationCertainty
from auto_coder.management.beta_runtime_smoke import BetaRuntimeSmokeProducer, SmokeProfile
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, AdmissionStatus, EffectReceipt, ManagedTarget, MutationStatus, OperationIntent, OperationPhase, OperationRecord
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkResult
from tests.test_beta_source_selection import SHA_A, SHA_B, selector
from tests.test_management_worker_lifecycle import GitEvidenceBoundary


@dataclass
class Clock:
    now: float = 1_798_000_000.0
    available: bool = True

    def __call__(self) -> float:
        if not self.available:
            raise OSError("controlled clock unavailability")
        return self.now


@dataclass
class WorkerBoundary:
    """Typed observations with explicit progress independent of reads."""

    config: WorkerEndpointConfig = field(default_factory=lambda: WorkerEndpointConfig("beta-worker", "beta", "fixture/beta", "10.23.10.12", 17890, b"a" * 48))
    supervisor_generation: str = "supervisor-original"
    process_run_id: str = "process-original"
    deployment_generation: str = "deployment-original"
    runtime_id: str = "runtime-original"
    receipt_identity: str = "c" * 64
    lock_sha256: str = "e" * 64
    source_sha: str = SHA_A
    distribution_version: str = f"1.0+g{SHA_A}"
    sequence: int = 41
    heartbeat_available: bool = True
    heartbeat_stage: str = "producer:maintenance-sleep"
    startup_reconciled: bool | None = True
    startup_error: str | None = None
    daemon_serving: bool | None = True
    lifecycle_state: str = "running"
    gate_state: str = "running"
    unsettled_count: int = 0
    stop_requested: bool = False
    alive: bool = True
    supervisor_status: str = "RUNNING"
    source_available: bool = True
    version_available: bool = True
    source_origin: str = "build_embedded"
    version_origin: str = "installed_distribution_metadata"
    binding_status: str = "VERIFIED"
    unavailable: bool = False
    stale_nonce: bool = False
    calls: list[str] = field(default_factory=list)
    nonces: list[str] = field(default_factory=list)
    on_observation: Callable[[], None] | None = None

    def binding(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "content_identity": self.receipt_identity,
            "lock_sha256": self.lock_sha256,
            "source_sha": self.source_sha,
            "distribution_version": self.distribution_version,
            "process_run_id": self.process_run_id,
            "deployment_generation": self.deployment_generation,
        }

    def worker(self) -> dict[str, object]:
        return {
            "process_run_id": self.process_run_id,
            "deployment_generation": self.deployment_generation,
            "runtime_id": self.runtime_id,
            "binding_status": self.binding_status,
            "runtime_binding": self.binding() if self.binding_status == "VERIFIED" else None,
            "stop_requested": self.stop_requested,
        }

    def result(self, data: dict[str, object]) -> WorkerNetworkResult:
        return WorkerNetworkResult(503 if self.unavailable else 200, "UNAVAILABLE" if self.unavailable else "OK", None if self.unavailable else data, self.supervisor_generation, "request-" + str(len(self.calls)))

    def inspect_supervisor(self) -> WorkerNetworkResult:
        self.calls.append("inspect")
        return self.result({"supervisor_incarnation": self.supervisor_generation, "status": self.supervisor_status, "alive": self.alive, "selected_runtime_id": self.runtime_id, "worker": self.worker()})

    def observe_worker(self, nonce: str | None = None) -> WorkerNetworkResult:
        self.calls.append("observe")
        assert isinstance(nonce, str) and nonce
        self.nonces.append(nonce)
        observation = {
            "schema_version": 1,
            "repository": self.config.repository,
            "channel": self.config.channel,
            "nonce": "stale-nonce" if self.stale_nonce else nonce,
            "process_run_id": self.process_run_id,
            "deployment_generation": self.deployment_generation,
            "observation_time": "2026-10-27T10:00:00+00:00",
            "artifact": {
                "source_revision": {"value": self.source_sha if self.source_available else None, "origin": self.source_origin, "available": self.source_available, "reason": None if self.source_available else "embedded_record_missing"},
                "distribution_version": {"value": self.distribution_version if self.version_available else None, "origin": self.version_origin, "available": self.version_available, "reason": None if self.version_available else "distribution_not_installed"},
            },
            "lifecycle": {"state": self.lifecycle_state, "startup_reconciled": self.startup_reconciled, "startup_reconciliation_error": self.startup_error, "daemon_serving": self.daemon_serving},
            "invocations": {"state": self.gate_state, "unsettled_count": self.unsettled_count, "is_graceful_ready": False, "unsettled": [], "clipped": False},
            "heartbeat": {"available": self.heartbeat_available, "sequence": self.sequence, "stage": self.heartbeat_stage, "age_seconds": 0.2},
        }
        result = self.result({"observation": observation, "binding_status": self.binding_status, "runtime_binding": self.binding() if self.binding_status == "VERIFIED" else None})
        if self.on_observation is not None:
            callback, self.on_observation = self.on_observation, None
            callback()
        return result

    def diagnostics(self, kind: str, params: dict[str, str] | None = None) -> WorkerNetworkResult:
        self.calls.append("diagnostics")
        return self.result({"status": "OK"})


@dataclass
class Harness:
    journal: ManagementOperationStore
    remote: GitEvidenceBoundary
    reader: BetaEvidenceReader
    worker: WorkerBoundary
    clock: Clock
    profile: SmokeProfile
    authority: AcceptedAuthority
    deployment: OperationRecord
    operation: OperationRecord

    def producer(self, *, journal: ManagementOperationStore | None = None, profile: SmokeProfile | None = None) -> BetaRuntimeSmokeProducer:
        return BetaRuntimeSmokeProducer(journal or self.journal, self.worker, self.reader, profile or self.profile, clock=self.clock)  # type: ignore[arg-type]

    def advance(self):
        return self.producer().advance(self.operation.operation_id)

    def next_heartbeat(self) -> None:
        self.worker.sequence += 1
        self.clock.now += 1.1

    def snapshot(self) -> dict[str, object]:
        saved = self.journal.smoke_snapshot(self.operation.operation_id)
        assert saved is not None
        return saved[1]


def _fixture(tmp_path: Path, *, selected_execution: tuple[int, int] = (11, 1)) -> Harness:
    journal = ManagementOperationStore(tmp_path / "journal")
    remote = GitEvidenceBoundary()
    if selected_execution != (11, 1):
        remote.executions[(selected_execution[0], 1)] = (SHA_A, "2026-10-02T01:00:00Z")
        remote.executions[selected_execution] = (SHA_A, "2026-10-02T02:00:00Z")
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    worker = WorkerBoundary()
    authority = AcceptedAuthority("https://manager.example", "https://manager.example/mcp", "project", "operator", "client", "original-grant", ("auto-coder:read", "auto-coder:deploy"), 1_797_999_900.0, 1_797_999_950.0)
    target = ManagedTarget(worker.config.instance_id, worker.config.channel, worker.config.repository)
    staged = {"source_sha": worker.source_sha, "lock_sha256": worker.lock_sha256, "distribution_version": worker.distribution_version, "content_identity": worker.receipt_identity, "installed_runtime_id": worker.runtime_id, "runtime_profile": "python-3.12"}
    deployed_snapshot = {
        "schema_version": 1,
        "stage": "SUCCEEDED",
        "action": "deploy_beta_source",
        "accepted_evidence": reader.read(SHA_A).canonical(),
        "source_sha": SHA_A,
        "staged": staged,
        "new_worker": worker.worker(),
        "incumbent": {"supervisor_generation": worker.supervisor_generation},
        "steps": {"prepare": {"state": "CONFIRMED", "observation": {"status": "READY", **staged}}, "start": {"state": "CONFIRMED", "observation": {"status": "RUNNING", "worker": worker.worker()}}},
        "heartbeats": [39, 40],
        "original_deadline": 1_798_000_100.0,
    }
    admitted = journal.admit(OperationIntent(target, "deploy_beta_source", (ActionParameter("source_sha", SHA_A),), "deployment-key"), authority, lifecycle_snapshot=deployed_snapshot)
    assert admitted.status is AdmissionStatus.CREATED and admitted.record is not None
    completed = journal.transition(admitted.record.operation_id, admitted.record.revision, OperationPhase.SUCCEEDED, evidence=EffectReceipt(admitted.record.operation_id, "supervisor-and-worker-lifecycle-journal", 1_797_999_999.0, "real_installed_worker_and_advancing_producer_heartbeats_verified"))
    assert completed.status is MutationStatus.UPDATED and completed.record is not None
    parameters = (ActionParameter("source_sha", SHA_A), ActionParameter("deployment_operation_id", completed.record.operation_id), ActionParameter("supervisor_generation", worker.supervisor_generation), ActionParameter("deployment_generation", worker.deployment_generation))
    accepted = journal.admit(OperationIntent(target, "run_beta_acceptance", parameters, "acceptance-key", worker.process_run_id, worker.runtime_id), authority)
    assert accepted.status is AdmissionStatus.CREATED and accepted.record is not None
    remote.calls.clear()
    return Harness(journal, remote, reader, worker, Clock(), SmokeProfile(total_seconds=5.0, interval_seconds=1.0), authority, completed.record, accepted.record)


@pytest.fixture
def smoke(tmp_path: Path) -> Harness:
    return _fixture(tmp_path)


def _terminal(observation, verdict: str) -> dict[str, object]:
    assert observation.state.value == "COMPLETE", observation
    assert observation.evidence is not None
    assert observation.evidence.local_result.value == verdict
    assert observation.evidence.content_sha256 == hashlib.sha256(observation.evidence.content.encode()).hexdigest()
    result = observation.evidence.document()
    assert result["schema_version"] == 1
    assert result["local_result"] == verdict
    return result


def _pass(smoke: Harness):
    first = smoke.advance()
    assert first.state.value == "IN_PROGRESS", first
    smoke.next_heartbeat()
    completed = smoke.advance()
    _terminal(completed, "PASS")
    return completed


@pytest.mark.parametrize("selected_execution", ((11, 1), (12, 1), (12, 2)))
def test_exact_original_proof_and_later_selected_execution_can_pass_without_publication(tmp_path: Path, selected_execution: tuple[int, int]) -> None:
    smoke = _fixture(tmp_path, selected_execution=selected_execution)
    original = smoke.reader.read(SHA_A)
    assert (original.proof_run_id, original.proof_run_attempt) == (101, 1)
    assert (original.selected_run_id, original.selected_run_attempt) == selected_execution
    smoke.remote.calls.clear()
    completed = _pass(smoke)
    snapshot = smoke.snapshot()
    assert len(snapshot["samples"]) == 2
    encoded = json.dumps(snapshot["capture"], sort_keys=True)
    assert original.selection_oid in encoded
    assert original.tested_source_oid in encoded
    assert original.selection_operation_id in encoded
    assert smoke.deployment.operation_id in encoded
    assert smoke.worker.runtime_id in encoded
    assert smoke.worker.receipt_identity in encoded
    assert smoke.worker.lock_sha256 in encoded
    assert "build_embedded" in encoded and "installed_distribution_metadata" in encoded
    assert len(set(smoke.worker.nonces)) == 2
    assert all(method == "GET" for method, _path in smoke.remote.calls)
    assert set(smoke.worker.calls) == {"inspect", "observe"}
    assert len(smoke.journal.list_operations().records) == 2
    assert smoke.journal.get(smoke.operation.operation_id).record == smoke.operation
    assert "PASS_PUBLISHED" not in completed.evidence.content


def test_diagnostics_and_rapid_polls_cannot_supply_heartbeat_progress(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    before = smoke.snapshot()
    for _ in range(5):
        smoke.worker.diagnostics("status")
        smoke.clock.now += 0.1
        assert smoke.advance().state.value == "IN_PROGRESS"
    assert smoke.worker.calls.count("observe") == 1
    assert smoke.snapshot()["samples"] == before["samples"]
    smoke.clock.now += 0.6
    assert smoke.advance().state.value == "IN_PROGRESS"
    assert smoke.worker.sequence == 41
    smoke.clock.now += 5
    _terminal(smoke.advance(), "BLOCKED")


def test_progress_after_original_deadline_cannot_be_sampled(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    original = smoke.snapshot()
    smoke.clock.now += 10
    smoke.worker.sequence += 10
    observation_count = smoke.worker.calls.count("observe")
    _terminal(smoke.advance(), "BLOCKED")
    assert smoke.worker.calls.count("observe") == observation_count
    assert smoke.snapshot()["window"] == original["window"]
    assert len(smoke.snapshot()["samples"]) == 1


@pytest.mark.parametrize("expired", (False, True))
def test_manager_reconstruction_preserves_capture_window_authority_and_profile(smoke: Harness, tmp_path: Path, expired: bool) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    original = smoke.snapshot()
    smoke.worker.sequence += 1
    smoke.clock.now += 7 if expired else 1.1
    store = ManagementOperationStore(tmp_path / "journal")
    refreshed = replace(smoke.authority, trusted_grant_id="refreshed-grant", authentication_time=smoke.clock.now, accepted_at=smoke.clock.now)
    replay = store.admit(smoke.operation.intent, refreshed)
    assert replay.status is AdmissionStatus.REPLAY
    assert replay.record == smoke.operation
    changed_profile = SmokeProfile(total_seconds=300, interval_seconds=0.01)
    resumed = smoke.producer(journal=store, profile=changed_profile).advance(smoke.operation.operation_id)
    _terminal(resumed, "BLOCKED" if expired else "PASS")
    after = smoke.snapshot()
    assert after["capture"] == original["capture"]
    assert after["profile"] == original["profile"]
    assert after["window"] == original["window"]
    assert after["binding"] == original["binding"]
    assert store.get(smoke.operation.operation_id).record.authority == smoke.authority


def test_terminal_replay_does_not_read_clock_worker_source_or_adopt_new_profile(smoke: Harness, tmp_path: Path) -> None:
    completed = _pass(smoke)
    before = smoke.snapshot()
    source_calls, worker_calls = list(smoke.remote.calls), list(smoke.worker.calls)
    smoke.remote.refs.clear()
    smoke.worker.unavailable = True
    smoke.clock.available = False
    rebuilt = BetaRuntimeSmokeProducer(ManagementOperationStore(tmp_path / "journal"), smoke.worker, smoke.reader, None, clock=smoke.clock)  # type: ignore[arg-type]
    assert rebuilt.read(smoke.operation.operation_id) == completed
    assert rebuilt.advance(smoke.operation.operation_id) == completed
    assert smoke.snapshot() == before
    assert smoke.remote.calls == source_calls
    assert smoke.worker.calls == worker_calls
    detached = completed.evidence.document()
    detached["local_result"] = "FAIL"
    assert rebuilt.read(smoke.operation.operation_id).evidence.local_result.value == "PASS"


def test_changed_input_conflicts_without_replacing_original_capture(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    before = smoke.snapshot()
    changed = replace(smoke.operation.intent, expected_artifact="different-runtime")
    result = smoke.journal.admit(changed, smoke.authority)
    assert result.status is AdmissionStatus.CONFLICT
    assert result.record == smoke.operation
    assert smoke.snapshot() == before
    smoke.next_heartbeat()
    _terminal(smoke.advance(), "PASS")


@pytest.mark.parametrize("selected_execution", ((12, 1), (12, 2)))
def test_confirmed_same_sha_selection_replaces_unfinished_window(smoke: Harness, tmp_path: Path, selected_execution: tuple[int, int]) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    original = smoke.snapshot()
    smoke.remote.executions[(selected_execution[0], 1)] = (SHA_A, "2026-10-03T02:00:00Z")
    smoke.remote.executions[selected_execution] = (SHA_A, "2026-10-03T03:00:00Z")
    assert selector(smoke.remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED
    smoke.next_heartbeat()
    resumed = smoke.producer(journal=ManagementOperationStore(tmp_path / "journal")).advance(smoke.operation.operation_id)
    _terminal(resumed, "SUPERSEDED")
    after = smoke.snapshot()
    assert after["capture"] == original["capture"]
    assert after["window"] == original["window"]
    assert len(after["samples"]) == 1


def test_newer_unselected_ci_execution_does_not_supersede_original_selection(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    original = smoke.snapshot()["capture"]
    smoke.remote.executions[(12, 2)] = (SHA_A, "2026-10-03T03:00:00Z")
    smoke.next_heartbeat()
    _terminal(smoke.advance(), "PASS")
    assert smoke.snapshot()["capture"] == original
    assert not any("workflows/test-source-for-runtime.yml/runs" in path for method, path in smoke.remote.calls)


def test_confirmed_different_source_selection_supersedes_original_window(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    smoke.remote.add_proof(SHA_B, "d" * 40, 102, 1)
    smoke.remote.executions[(12, 1)] = (SHA_B, "2026-10-03T03:00:00Z")
    assert selector(smoke.remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED
    smoke.next_heartbeat()
    _terminal(smoke.advance(), "SUPERSEDED")


def test_changed_selection_during_observation_cannot_qualify_sample(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    original_samples = smoke.snapshot()["samples"]

    def replace_selection() -> None:
        smoke.remote.executions[(12, 1)] = (SHA_A, "2026-10-03T03:00:00Z")
        assert selector(smoke.remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED

    smoke.worker.on_observation = replace_selection
    smoke.next_heartbeat()
    _terminal(smoke.advance(), "SUPERSEDED")
    assert [sample for sample in smoke.snapshot()["samples"] if sample.get("qualifying") is True] == original_samples


def test_selection_must_match_the_exact_successful_deployment(smoke: Harness) -> None:
    smoke.remote.executions[(12, 1)] = (SHA_A, "2026-10-03T03:00:00Z")
    assert selector(smoke.remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED
    _terminal(smoke.advance(), "BLOCKED")
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


@pytest.mark.parametrize("attempt", ((101, 1), (11, 1)))
@pytest.mark.parametrize("fault", ("missing_jobs", "failed", "incomplete"))
def test_unavailable_or_unsuccessful_proof_and_selected_execution_block_before_sampling(smoke: Harness, attempt: tuple[int, int], fault: str) -> None:
    if fault == "missing_jobs":
        smoke.remote.missing_jobs.add(attempt)
    elif fault == "failed":
        smoke.remote.failed_attempts.add(attempt)
    else:
        smoke.remote.attempt_states[attempt] = ("in_progress", None)
    _terminal(smoke.advance(), "BLOCKED")
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])
    assert "observe" not in smoke.worker.calls


def test_unavailable_selection_is_blocked_not_a_confirmed_replacement(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    smoke.remote.refs.pop("refs/tags/auto-coder-beta-selection-current")
    smoke.next_heartbeat()
    _terminal(smoke.advance(), "BLOCKED")


@pytest.mark.parametrize("field_name,new_value", (("runtime_id", "same-sha-other-runtime"), ("receipt_identity", "d" * 64), ("lock_sha256", "f" * 64)))
def test_initial_same_sha_runtime_or_ready_lock_mismatch_is_blocked(smoke: Harness, field_name: str, new_value: str) -> None:
    setattr(smoke.worker, field_name, new_value)
    _terminal(smoke.advance(), "BLOCKED")
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


@pytest.mark.parametrize("field_name,new_value", (("supervisor_generation", "supervisor-replacement"), ("process_run_id", "process-replacement"), ("deployment_generation", "deployment-replacement"), ("runtime_id", "runtime-replacement"), ("receipt_identity", "d" * 64), ("lock_sha256", "f" * 64)))
def test_observed_incarnation_or_runtime_replacement_supersedes_original_capture(smoke: Harness, field_name: str, new_value: str) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    before = smoke.snapshot()
    setattr(smoke.worker, field_name, new_value)
    smoke.next_heartbeat()
    _terminal(smoke.advance(), "SUPERSEDED")
    assert smoke.snapshot()["capture"] == before["capture"]
    assert [sample for sample in smoke.snapshot()["samples"] if sample.get("qualifying") is True] == before["samples"]


@pytest.mark.parametrize(
    "field_name,new_value", (("source_available", False), ("version_available", False), ("source_origin", "declared_configuration"), ("version_origin", "desired_package_version"), ("binding_status", "UNAVAILABLE"), ("heartbeat_available", False), ("unavailable", True), ("stale_nonce", True))
)
def test_missing_or_non_authoritative_worker_evidence_is_blocked(smoke: Harness, field_name: str, new_value: object) -> None:
    setattr(smoke.worker, field_name, new_value)
    _terminal(smoke.advance(), "BLOCKED")
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


@pytest.mark.parametrize("field_name,new_value", (("startup_reconciled", False), ("startup_error", "reconciliation_failed"), ("daemon_serving", False), ("lifecycle_state", "draining"), ("gate_state", "draining"), ("stop_requested", True), ("alive", False)))
def test_actual_required_predicate_violation_fails(smoke: Harness, field_name: str, new_value: object) -> None:
    setattr(smoke.worker, field_name, new_value)
    _terminal(smoke.advance(), "FAIL")
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


@pytest.mark.parametrize("field_name", ("startup_reconciled", "daemon_serving"))
def test_absent_predicate_evidence_is_blocked_instead_of_failed(smoke: Harness, field_name: str) -> None:
    setattr(smoke.worker, field_name, None)
    _terminal(smoke.advance(), "BLOCKED")


def test_normal_active_work_is_not_an_unsettled_lifecycle_change(smoke: Harness) -> None:
    smoke.worker.unsettled_count = 2
    _pass(smoke)


def test_journal_holds_target_lane_while_update_is_unresolved(tmp_path: Path) -> None:
    journal = ManagementOperationStore(tmp_path)
    target = ManagedTarget("beta-worker", "beta", "fixture/beta")
    authority = AcceptedAuthority("issuer", "resource", "project", "uid", "client", "grant", ("read", "deploy"), 1, 1)
    updating = journal.admit(OperationIntent(target, "deploy_beta_source", (ActionParameter("source_sha", SHA_A),), "update"), authority)
    assert updating.record is not None
    assert journal.transition(updating.record.operation_id, 0, OperationPhase.NEEDS_ATTENTION, safe_error_category="unresolved_update").status is MutationStatus.UPDATED
    acceptance = journal.admit(OperationIntent(target, "run_beta_acceptance", (ActionParameter("source_sha", SHA_A),), "accept"), authority)
    assert acceptance.status is AdmissionStatus.BUSY
    assert acceptance.record.operation_id == updating.record.operation_id
    assert len(journal.list_operations().records) == 1


def test_unresolved_deployment_snapshot_is_not_ready_even_when_operation_row_succeeded(smoke: Harness) -> None:
    saved = smoke.journal.lifecycle_snapshot(smoke.deployment.operation_id)
    assert saved is not None
    revision, snapshot = saved
    snapshot["stage"] = "RECONCILING"
    assert smoke.journal.lifecycle_save(smoke.deployment.operation_id, revision, snapshot)
    _terminal(smoke.advance(), "BLOCKED")
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


@pytest.mark.parametrize("kwargs", ({"total_seconds": 0}, {"total_seconds": -1}, {"total_seconds": math.inf}, {"total_seconds": math.nan}, {"interval_seconds": 0}, {"interval_seconds": -1}, {"interval_seconds": math.inf}, {"minimum_samples": 1}))
def test_invalid_profile_blocks_before_worker_sampling(smoke: Harness, kwargs: dict[str, object]) -> None:
    invalid = SmokeProfile(**kwargs)
    producer = BetaRuntimeSmokeProducer(smoke.journal, smoke.worker, smoke.reader, invalid, clock=smoke.clock)  # type: ignore[arg-type]
    _terminal(producer.advance(smoke.operation.operation_id), "BLOCKED")
    assert not smoke.worker.calls
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


def test_missing_profile_blocks_without_sampling(smoke: Harness) -> None:
    producer = BetaRuntimeSmokeProducer(smoke.journal, smoke.worker, smoke.reader, None, clock=smoke.clock)  # type: ignore[arg-type]
    _terminal(producer.advance(smoke.operation.operation_id), "BLOCKED")
    assert not smoke.worker.calls


@pytest.mark.parametrize("after_capture", (False, True))
def test_unavailable_clock_cannot_start_or_extend_sampling(smoke: Harness, after_capture: bool) -> None:
    if after_capture:
        assert smoke.advance().state.value == "IN_PROGRESS"
    observations = len(smoke.worker.calls)
    smoke.clock.available = False
    _terminal(smoke.advance(), "BLOCKED")
    assert len(smoke.worker.calls) == observations


def test_clock_rollback_is_blocked_without_restarting_original_window(smoke: Harness) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    window = smoke.snapshot()["window"]
    smoke.clock.now -= 5
    _terminal(smoke.advance(), "BLOCKED")
    assert smoke.snapshot()["window"] == window


def test_failed_result_write_does_not_expose_durable_pass(smoke: Harness, monkeypatch) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    smoke.next_heartbeat()

    def fail_write(*_args, **_kwargs) -> bool:
        raise sqlite3.OperationalError("controlled full disk")

    monkeypatch.setattr(smoke.journal, "smoke_save", fail_write)
    failed = smoke.advance()
    assert failed.state.value == "UNAVAILABLE"
    assert failed.evidence is None
    persisted = smoke.snapshot()
    assert persisted["result"] is None
    assert len(persisted["samples"]) == 1


def test_lost_terminal_acknowledgement_returns_exact_record_after_reconstruction(smoke: Harness, tmp_path: Path, monkeypatch) -> None:
    assert smoke.advance().state.value == "IN_PROGRESS"
    smoke.next_heartbeat()
    save = smoke.journal.smoke_save
    snapshot = smoke.journal.smoke_snapshot
    uncertain = False

    def lose_terminal_ack(operation_id: str, revision: int, document: dict[str, object]) -> bool:
        nonlocal uncertain
        result = save(operation_id, revision, document)
        if document["result"] is not None:
            uncertain = True
            raise sqlite3.OperationalError("controlled terminal acknowledgement loss")
        return result

    def uncertain_read(operation_id: str):
        if uncertain:
            raise sqlite3.OperationalError("controlled readback unavailability")
        return snapshot(operation_id)

    monkeypatch.setattr(smoke.journal, "smoke_save", lose_terminal_ack)
    monkeypatch.setattr(smoke.journal, "smoke_snapshot", uncertain_read)
    unavailable = smoke.advance()
    assert unavailable.state.value == "UNAVAILABLE" and unavailable.evidence is None
    reads = len(smoke.worker.calls)
    restored = smoke.producer(journal=ManagementOperationStore(tmp_path / "journal"))
    completed = restored.read(smoke.operation.operation_id)
    _terminal(completed, "PASS")
    assert restored.advance(smoke.operation.operation_id) == completed
    assert len(smoke.worker.calls) == reads


def test_unknown_operation_does_not_create_a_new_operation_or_collect_evidence(smoke: Harness) -> None:
    producer = smoke.producer()
    assert producer.read("unknown-operation").state.value == "NOT_FOUND"
    assert producer.advance("unknown-operation").state.value == "NOT_FOUND"
    assert len(smoke.journal.list_operations().records) == 2
    assert not smoke.worker.calls and not smoke.remote.calls


def test_configured_minimum_samples_requires_every_distinct_heartbeat(smoke: Harness) -> None:
    smoke.profile = SmokeProfile(total_seconds=6, interval_seconds=1, minimum_samples=3)
    assert smoke.advance().state.value == "IN_PROGRESS"
    smoke.next_heartbeat()
    assert smoke.advance().state.value == "IN_PROGRESS"
    smoke.next_heartbeat()
    completed = smoke.advance()
    document = _terminal(completed, "PASS")
    samples = document["samples"]
    assert [sample["heartbeat"]["sequence"] for sample in samples] == [41, 42, 43]
    assert all(sample["qualifying"] is True for sample in samples)
    assert document["completed_at"] < document["window"]["deadline"]
    for sample in samples:
        checks = {check["check"]: check["disposition"] for check in sample["checks"]}
        assert set(smoke.profile.required_predicates) <= checks.keys()
        assert all(checks[name] == "PASS" for name in smoke.profile.required_predicates)


def test_selected_completion_time_must_still_equal_required_jobs(smoke: Harness) -> None:
    smoke.remote.executions[(11, 1)] = (SHA_A, "2026-10-02T02:00:00Z")
    _terminal(smoke.advance(), "BLOCKED")
    assert not smoke.worker.calls


def test_manager_or_diagnostic_stage_is_not_a_producer_heartbeat(smoke: Harness) -> None:
    smoke.worker.heartbeat_stage = "diagnostic:nonce-response"
    _terminal(smoke.advance(), "BLOCKED")
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


def test_non_acceptance_operation_cannot_run_internal_sampler(smoke: Harness) -> None:
    result = smoke.producer().advance(smoke.deployment.operation_id)
    assert result.state.value == "CONFLICT"
    assert smoke.journal.smoke_snapshot(smoke.deployment.operation_id) is None
    assert not smoke.worker.calls and not smoke.remote.calls


def test_supervisor_store_error_projection_is_unavailable_not_confirmed_worker_failure(smoke: Harness, monkeypatch) -> None:
    def unreadable_supervisor() -> WorkerNetworkResult:
        return smoke.worker.result({"supervisor_incarnation": smoke.worker.supervisor_generation, "status": "NEEDS_ATTENTION", "alive": False, "worker": None})

    monkeypatch.setattr(smoke.worker, "inspect_supervisor", unreadable_supervisor)
    completed = smoke.advance()
    _terminal(completed, "BLOCKED")
    assert "observe" not in smoke.worker.calls
    assert not any(sample.get("qualifying") is True for sample in smoke.snapshot()["samples"])


def test_running_supervisor_may_omit_optional_stop_requested_flag(smoke: Harness, monkeypatch) -> None:
    original = smoke.worker.worker

    def unrequested_worker() -> dict[str, object]:
        value = original()
        value.pop("stop_requested")
        return value

    monkeypatch.setattr(smoke.worker, "worker", unrequested_worker)
    _pass(smoke)


@pytest.mark.parametrize("missing_provenance", (False, True))
def test_first_unsuccessful_sample_preserves_every_available_capture_binding(smoke: Harness, missing_provenance: bool) -> None:
    original_source = json.loads(smoke.reader.read(SHA_A).canonical())
    if missing_provenance:
        smoke.worker.source_available = False
    else:
        smoke.worker.daemon_serving = False
    document = _terminal(smoke.advance(), "BLOCKED" if missing_provenance else "FAIL")
    capture = document["capture"]
    assert capture["source"] == original_source
    assert capture["deployment"]["operation_id"] == smoke.deployment.operation_id
    assert capture["deployment"]["accepted_evidence"] == original_source
    assert smoke.snapshot()["capture"] == capture
    if missing_provenance:
        assert capture["worker"] is None
    else:
        assert capture["worker"]["runtime_id"] == smoke.worker.runtime_id
        assert capture["worker"]["process_run_id"] == smoke.worker.process_run_id
        assert capture["worker"]["ready_content_identity"] == smoke.worker.receipt_identity
        assert capture["worker"]["source_revision"]["value"] == SHA_A
