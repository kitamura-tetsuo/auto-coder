"""Durable handoff and exact, bounded Supervisor recovery regressions."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.bootstrap_supervisor import ResultStatus
from auto_coder.management.self_update_handoff import ApplicationProof, HandoffPhase, HandoffStore, SelfUpdateHandoff, observe_supervisor
from auto_coder.management.version_staging import ManagementReadyReceipt
from tests.test_management_bootstrap_supervisor import _await_child_ready, _resign_runtime, _runtime, _stop_process, _supervisor


@pytest.fixture
def _use_real_sleep() -> None:
    """Keep actual process-lifetime observation and contention barriers."""


@pytest.fixture
def _use_real_commands() -> None:
    """Keep real subprocess execution at the Supervisor boundary."""


def _receipt(name: str) -> ManagementReadyReceipt:
    values = {field: name for field in ManagementReadyReceipt.__dataclass_fields__}
    values.update(schema_version=1, instance_id="manager", test_run_id=1, test_run_attempt=1)
    return ManagementReadyReceipt(**values)


def _handoff(operation: str = "operation") -> SelfUpdateHandoff:
    return SelfUpdateHandoff(operation, "attempt", "correlation", 3, "incarnation", ChildRecord("manager", ChildPhase.RUNNING, "old", pid=123, start_token="456", child_generation=1), _receipt("candidate"), _receipt("old"), "sha256:" + "a" * 64, time.time(), "b" * 64)


def test_mailbox_commit_loss_replays_exact_handoff_without_replacing_authority(tmp_path: Path) -> None:
    class LostAcknowledgement(HandoffStore):
        def _after_submit_commit(self) -> None:
            raise OSError("injected acknowledgement loss")

    handoff = _handoff()
    with pytest.raises(OSError):
        LostAcknowledgement(tmp_path, "manager").submit(handoff)
    recovered = HandoffStore(tmp_path, "manager")
    original = recovered.read(handoff.operation_id)
    assert original is not None and original.handoff == handoff
    assert recovered.submit(handoff) == original
    with pytest.raises(ValueError, match="conflicts"):
        recovered.submit(replace(handoff, expected_supervisor="new-authority"))
    assert recovered.read(handoff.operation_id) == original


def test_mailbox_concurrent_updates_reach_same_reserved_lane(tmp_path: Path, _use_real_sleep: None) -> None:
    store = HandoffStore(tmp_path, "manager")
    store.active()
    barrier = threading.Barrier(2, timeout=10)
    outcomes: list[str] = []

    def submit(operation: str) -> None:
        barrier.wait()
        try:
            HandoffStore(tmp_path, "manager").submit(_handoff(operation))
            outcomes.append("accepted")
        except ValueError:
            outcomes.append("busy")

    threads = [threading.Thread(target=submit, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["accepted", "busy"]
    assert len(store.active()) == 1


def test_unknown_handoff_preserves_exclusion_and_original_receipts(tmp_path: Path) -> None:
    store = HandoffStore(tmp_path, "manager")
    record = store.submit(_handoff())
    unknown = store.change(record, phase=HandoffPhase.NEEDS_ATTENTION, detail="lost process evidence")
    assert unknown is not None
    assert store.change(record, phase=HandoffPhase.SUCCEEDED) is None
    with pytest.raises(ValueError, match="retains the process lane"):
        store.submit(_handoff("another"))
    assert HandoffStore(tmp_path, "manager").read(record.handoff.operation_id) == unknown


def test_authenticated_proof_requires_selected_lifetime_runtime_and_bounded_time(tmp_path: Path) -> None:
    store = HandoffStore(tmp_path, "manager")
    submitted = store.submit(_handoff())
    now = time.time()
    awaiting = store.change(submitted, phase=HandoffPhase.AWAITING_PROOF, selected_identity="123:456:2:candidate:incarnation", selected_generation=2, deadline=now + 30)
    assert awaiting is not None
    proof = ApplicationProof(awaiting.selected_identity, "candidate", "candidate", "candidate", "uid", "client", "grant", now)
    assert not store.record_proof(submitted.handoff.operation_id, replace(proof, child_identity="old-generation"))
    assert not store.record_proof(submitted.handoff.operation_id, replace(proof, source_sha="another-source"))
    assert not store.record_proof(submitted.handoff.operation_id, replace(proof, observed_at=now + 60))
    assert store.record_proof(submitted.handoff.operation_id, proof)
    recorded = store.read(submitted.handoff.operation_id)
    assert recorded is not None and recorded.proof == proof
    assert recorded.phase is HandoffPhase.AWAITING_PROOF
    assert not recorded.challenge_observed


def test_supervisor_observation_is_read_only_and_requires_live_presence(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    assert observe_supervisor(supervisor.config) is None
    assert not supervisor.store.path.exists()
    started = supervisor.start_selected("start", "attempt", runtime.reference)
    try:
        _await_child_ready(runtime)
        assert observe_supervisor(supervisor.config) is None
        supervisor.store.publish_supervisor(supervisor.incarnation, os.getpid(), supervisor._start_token(os.getpid()))
        observed = observe_supervisor(supervisor.config)
        assert observed == (supervisor.incarnation, started.record)
        with sqlite3.connect(supervisor.store.path) as connection:
            connection.execute("UPDATE supervisor_presence SET start_token='stale'")
        assert observe_supervisor(supervisor.config) is None
    finally:
        _stop_process(started.record.pid)


def test_failed_application_safety_restore_is_exact_and_once(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("initial", "initial-attempt", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("switch-stop", "switch", supervisor._identity(first.record)).status is ResultStatus.STOPPED
    started = supervisor.start_selected("switch-start", "switch", "candidate")
    _await_child_ready(candidate)
    assert started.status is ResultStatus.OBSERVED_RUNNING
    stopped = supervisor.request_graceful_stop("restore-stop", "restore", supervisor._identity(started.record))
    assert stopped.status is ResultStatus.STOPPED
    restored = supervisor.restore_previous("restore", "restore", supervisor._identity(stopped.record), "old")
    try:
        assert restored.status is ResultStatus.PROCESS_RESTORED
        assert restored.record.selected_runtime == "old" and restored.record.recovery_used
        assert restored.record.pid not in {first.record.pid, started.record.pid}
        assert not restored.record.intended_stop
        replay = supervisor.restore_previous("restore", "restore", supervisor._identity(stopped.record), "old")
        assert replay.record == restored.record
        assert supervisor.inspect().record == restored.record
        assert supervisor.restore_previous("another-restore", "another", supervisor._identity(restored.record), "old").status is ResultStatus.UNKNOWN
        assert supervisor.inspect().record.pid == restored.record.pid
    finally:
        _stop_process(restored.record.pid)


def test_previous_runtime_immediate_exit_is_not_claimed_restored(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("initial", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("stop", "b", supervisor._identity(first.record)).status is ResultStatus.STOPPED
    started = supervisor.start_selected("candidate", "c", "candidate")
    _await_child_ready(candidate)
    stopped = supervisor.request_graceful_stop("candidate-stop", "d", supervisor._identity(started.record))
    assert stopped.status is ResultStatus.STOPPED
    old.executable.write_bytes(b"#!/bin/sh\nexit 21\n")
    replacement = _resign_runtime(old)
    supervisor.config = replace(supervisor.config, runtimes=(replacement, candidate))
    failed = supervisor.restore_previous("restore", "e", supervisor._identity(stopped.record), "old")
    assert failed.status is ResultStatus.NEEDS_ATTENTION
    assert failed.record.recovery_used and failed.record.pid == 0
    assert failed.record.exit_cause == "previous_runtime_start_failed"
    assert supervisor.restore_previous("restore", "e", supervisor._identity(stopped.record), "old").status is ResultStatus.NEEDS_ATTENTION


def test_new_update_has_its_own_one_time_recovery_budget(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    candidate.executable.write_bytes(b"#!/bin/sh\nexit 23\n")
    candidate = _resign_runtime(candidate)
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("initial", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("stop-1", "b", supervisor._identity(first.record)).status is ResultStatus.STOPPED
    restored = supervisor.start_selected("candidate-1", "c", "candidate")
    assert restored.status is ResultStatus.PROCESS_RESTORED
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("stop-2", "d", supervisor._identity(restored.record)).status is ResultStatus.STOPPED
    restored_again = supervisor.start_selected("candidate-2", "e", "candidate")
    try:
        assert restored_again.status is ResultStatus.PROCESS_RESTORED
        assert restored_again.record.recovery_used
        assert restored_again.record.pid != restored.record.pid
    finally:
        _stop_process(restored_again.record.pid)


@pytest.mark.parametrize("phase", (HandoffPhase.STOPPING, HandoffPhase.STARTING, HandoffPhase.RESTORING, HandoffPhase.RESTORE_STARTING))
def test_crash_after_persisted_effect_claim_never_reissues_process_action(tmp_path: Path, phase: HandoffPhase, _use_real_sleep: None, _use_real_commands: None) -> None:
    from auto_coder.management.self_update_handoff import SelfUpdateHandoffConsumer

    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, old, candidate)
    original = supervisor.start_selected("initial", "initial-attempt", "old")
    _await_child_ready(old)
    handoff = replace(_handoff(), expected_supervisor=supervisor.incarnation, expected_child=original.record)
    store = HandoffStore(supervisor.config.state_root, "manager")
    submitted = store.submit(handoff)
    live_pid = original.record.pid
    if phase is not HandoffPhase.STOPPING:
        stopped = supervisor.request_graceful_stop("operation:stop", "attempt", supervisor._identity(original.record))
        assert stopped.status is ResultStatus.STOPPED
        live_pid = 0
    if phase in {HandoffPhase.RESTORING, HandoffPhase.RESTORE_STARTING}:
        started = supervisor.start_selected("operation:start", "attempt", "candidate")
        assert started.status is ResultStatus.OBSERVED_RUNNING
        _await_child_ready(candidate)
        live_pid = started.record.pid
        if phase is HandoffPhase.RESTORE_STARTING:
            stopped = supervisor.request_graceful_stop("operation:restore-stop", "attempt", supervisor._identity(started.record))
            assert stopped.status is ResultStatus.STOPPED
            live_pid = 0

    class LostPhaseAcknowledgement(HandoffStore):
        def _after_phase_commit(self, record) -> None:
            if record.phase is phase:
                raise OSError("injected process loss after durable effect claim")

    with pytest.raises(OSError):
        LostPhaseAcknowledgement(supervisor.config.state_root, "manager").change(submitted, phase=phase)
    persisted = store.read(handoff.operation_id)
    assert persisted is not None and persisted.phase is phase
    with sqlite3.connect(supervisor.store.path) as connection:
        effects_before = connection.execute("SELECT operation_id,attempt_id,action,phase FROM effects ORDER BY operation_id").fetchall()
    before = supervisor.store.load()
    try:
        reconstructed = type(supervisor)(supervisor.config, "reconstructed")
        SelfUpdateHandoffConsumer(reconstructed).tick()
        final = store.read(handoff.operation_id)
        assert final is not None and final.phase is HandoffPhase.NEEDS_ATTENTION
        assert reconstructed.store.load() == before
        with sqlite3.connect(supervisor.store.path) as connection:
            assert connection.execute("SELECT operation_id,attempt_id,action,phase FROM effects ORDER BY operation_id").fetchall() == effects_before
        if live_pid:
            assert reconstructed._start_token(live_pid) == before.start_token
        assert store.active() == (final,)
    finally:
        _stop_process(live_pid)


def test_verified_success_is_not_reverted_on_later_unrelated_crash(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("initial", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("stop", "b", supervisor._identity(first.record)).status is ResultStatus.STOPPED
    started = supervisor.start_selected("candidate", "c", "candidate")
    _await_child_ready(candidate)
    verified = replace(started.record, readiness="application_ready")
    assert supervisor.store.reserve(started.record, verified)
    child = supervisor._children[started.record.pid]
    child.terminate()
    child.wait(timeout=5)
    observed = supervisor.reconcile()
    assert observed.status is ResultStatus.NEEDS_ATTENTION
    assert observed.record.pid == 0
    assert observed.record.child_generation == started.record.child_generation
    assert observed.record.selected_runtime == "candidate"
    assert not observed.record.recovery_used


def test_loop_exit_between_observations_preserves_one_time_safety_return(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor, Observation

    class RestoredObserved(Exception):
        pass

    class ExitAfterObservation(ManagementBootstrapSupervisor):
        watching_loop = False
        exit_injected = False
        deadline = 0.0

        def inspect(self) -> Observation:
            observed = super().inspect()
            if self.watching_loop:
                assert time.monotonic() < self.deadline, "loop bypassed the candidate exit safety return"
                if not self.exit_injected and observed.status is ResultStatus.OBSERVED_RUNNING and observed.record.selected_runtime == "candidate":
                    # Exercise the race with an actual process exit after its
                    # running observation, before the loop can observe again.
                    self.exit_injected = True
                    child = self._children[observed.record.pid]
                    child.terminate()
                    child.wait(timeout=5)
                elif observed.record.selected_runtime == "old" and observed.record.recovery_used:
                    raise RestoredObserved
            return observed

    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    supervisor = ExitAfterObservation(_supervisor(tmp_path, old, candidate).config)
    original = supervisor.start_selected("initial", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("stop", "b", supervisor._identity(original.record)).status is ResultStatus.STOPPED
    selected = supervisor.start_selected("candidate", "c", "candidate")
    assert selected.status is ResultStatus.OBSERVED_RUNNING
    _await_child_ready(candidate)
    supervisor.watching_loop = True
    supervisor.deadline = time.monotonic() + 3
    try:
        with pytest.raises(RestoredObserved):
            supervisor._run_owned()
        restored = supervisor.store.load()
        assert supervisor.exit_injected
        assert restored.selected_runtime == "old" and restored.recovery_used
        assert restored.child_generation == selected.record.child_generation + 1
        assert restored.pid not in {original.record.pid, selected.record.pid}
        assert supervisor._start_token(restored.pid) == restored.start_token
    finally:
        _stop_process(supervisor.store.load().pid)


def test_installed_candidate_retains_fixed_operator_service_environment(tmp_path: Path) -> None:
    from auto_coder.management.self_update_handoff import runtime_for_receipt

    initial = replace(_runtime(tmp_path, "initial"), environment=(("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "service-account.json")), ("PATH", "/fixed/toolchain/bin:/usr/bin"), ("PYTHONPATH", "/unrelated/source")))
    config = replace(_supervisor(tmp_path, initial).config, management_version_root=tmp_path / "versions", management_config=tmp_path / "management.yml")
    receipt = replace(_receipt("candidate"), environment_path=str(tmp_path / "versions" / "candidate" / "venv"), interpreter_path=str(tmp_path / "versions" / "candidate" / "venv" / "bin" / "python"))
    selected = runtime_for_receipt(config, receipt)
    environment = dict(selected.environment)
    assert environment["GOOGLE_APPLICATION_CREDENTIALS"] == str(tmp_path / "service-account.json")
    assert environment["PATH"] == "/fixed/toolchain/bin:/usr/bin"
    assert "PYTHONPATH" not in environment and "PYTHONHOME" not in environment
    assert selected.arguments == ("-I", "-B", "-m", "auto_coder.cli", "management", "serve", "--config", str(config.management_config))


def test_receipt_changed_after_real_old_stop_uses_one_previous_runtime_return(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("initial", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("stop", "b", supervisor._identity(first.record)).status is ResultStatus.STOPPED
    assert candidate.source_root is not None
    (candidate.source_root / "package.txt").write_text("changed after the old service was drained")
    result = supervisor.start_selected("candidate", "c", "candidate")
    try:
        assert result.status is ResultStatus.PROCESS_RESTORED
        assert result.record.selected_runtime == "old" and result.record.recovery_used
        assert result.record.pid != first.record.pid
        assert result.record.child_generation == first.record.child_generation + 2
        assert candidate.drain_ack is not None and not Path(str(candidate.drain_ack) + ".ready").exists()
        assert supervisor.start_selected("candidate", "c", "candidate").record == result.record
    finally:
        _stop_process(result.record.pid)


@pytest.mark.parametrize("reader", ("read", "active", "change", "runtime"))
def test_corrupt_mutable_handoff_cannot_change_original_intent(tmp_path: Path, reader: str) -> None:
    import json

    store = HandoffStore(tmp_path, "manager")
    record = store.submit(_handoff())
    with sqlite3.connect(store.path) as connection:
        raw = json.loads(connection.execute("SELECT record_json FROM handoffs").fetchone()[0])
        raw["handoff"]["candidate"]["source_sha"] = "a different source"
        connection.execute("UPDATE handoffs SET record_json=?", (json.dumps(raw),))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        if reader == "read":
            store.read(record.handoff.operation_id)
        elif reader == "active":
            store.active()
        elif reader == "change":
            store.change(record, phase=HandoffPhase.SUCCEEDED)
        else:
            store.runtime("candidate")


@pytest.mark.parametrize("field,value", (("source_sha", "wrong"), ("runtime_id", "wrong"), ("receipt_identity", "wrong"), ("child_identity", "old"), ("grant_id", ""), ("uid", "other"), ("client_id", "other"), ("observed_at", float("nan")), ("observed_at", 0)))
def test_persisted_success_proof_rejects_changed_or_incomplete_evidence(tmp_path: Path, field: str, value: object) -> None:
    from auto_coder.management.self_update_handoff import valid_application_proof

    store = HandoffStore(tmp_path, "manager")
    submitted = store.submit(_handoff())
    now = time.time()
    proof = ApplicationProof("selected", "candidate", "candidate", "candidate", "uid", "client", "grant", now)
    record = replace(submitted, phase=HandoffPhase.SUCCEEDED, selected_identity="selected", selected_generation=2, challenge_observed=True, deadline=now + 30, proof=proof)
    assert valid_application_proof(record, uid="uid", client_id="client")
    corrupted = replace(record, proof=replace(proof, **{field: value}))
    assert not valid_application_proof(corrupted, uid="uid", client_id="client")
    assert not valid_application_proof(replace(record, challenge_observed=False), uid="uid", client_id="client")


def test_revoked_forward_authority_can_return_only_exact_drained_original(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    old, candidate = _runtime(tmp_path, "old"), _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("initial", "a", "old")
    _await_child_ready(old)
    stopped = supervisor.request_graceful_stop("operation:stop", "b", supervisor._identity(first.record))
    assert stopped.status is ResultStatus.STOPPED
    restored = supervisor.restore_stopped_original("operation:restore", "c", supervisor._identity(stopped.record), "old")
    try:
        assert restored.status is ResultStatus.PROCESS_RESTORED
        assert restored.record.selected_runtime == "old"
        assert restored.record.recovery_used
        assert restored.record.pid != first.record.pid
        assert restored.record.child_generation == first.record.child_generation + 2
        assert candidate.drain_ack is not None and not Path(str(candidate.drain_ack) + ".ready").exists()
        assert supervisor.restore_stopped_original("operation:restore", "c", supervisor._identity(stopped.record), "old").record == restored.record
        with sqlite3.connect(supervisor.store.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM effects WHERE action='START_SELECTED'").fetchone() == (1,)
    finally:
        _stop_process(restored.record.pid)


def test_real_module_supervisor_uses_canonical_lifecycle_types(tmp_path: Path, _use_real_sleep: None, _use_real_commands: None) -> None:
    import json
    import subprocess
    import sys
    from dataclasses import asdict

    initial = _runtime(tmp_path, "old")
    config = replace(_supervisor(tmp_path, initial).config, management_version_root=tmp_path / "versions", management_config=tmp_path / "management.yml")
    payload = asdict(config)
    payload["runtimes"][0]["environment"] = dict(initial.environment)
    config_path = tmp_path / "bootstrap.json"
    config_path.write_text(json.dumps(payload, default=str))
    repository = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(repository / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    supervisor = subprocess.Popen([sys.executable, "-m", "auto_coder.management.bootstrap_supervisor", "--config", str(config_path)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = 0
    try:
        deadline = time.monotonic() + 10
        observed = None
        while time.monotonic() < deadline and observed is None:
            observed = observe_supervisor(config)
            time.sleep(0.02)
        assert observed is not None
        incarnation, child = observed
        child_pid = child.pid
        candidate = replace(_receipt("candidate"), environment_path=str(tmp_path / "versions" / "candidate" / "venv"), interpreter_path=str(tmp_path / "versions" / "candidate" / "venv" / "bin" / "python"))
        previous = replace(_receipt("old"), environment_path=str(tmp_path / "versions" / "old" / "venv"), interpreter_path=str(tmp_path / "versions" / "old" / "venv" / "bin" / "python"))
        store = HandoffStore(config.state_root, config.instance_id)
        submitted = store.submit(replace(_handoff(), expected_child=child, expected_supervisor=incarnation, candidate=candidate, previous=previous))
        record = submitted
        while time.monotonic() < deadline and record.phase is HandoffPhase.SUBMITTED:
            record = store.read(submitted.handoff.operation_id)
            assert record is not None
            time.sleep(0.02)
        assert record.phase is HandoffPhase.NEEDS_ATTENTION
        # A real matching child passes the identity gate. These deliberately
        # absent runtime receipts must fail at the following READY boundary.
        assert record.detail == "selected or previous installed READY identity is unavailable; no child was stopped"
        assert observe_supervisor(config) == (incarnation, child)
    finally:
        supervisor.terminate()
        supervisor.wait(timeout=5)
        _stop_process(child_pid)
