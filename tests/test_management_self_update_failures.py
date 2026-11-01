"""Authenticated self-update refusals and recovery with actual installed failures."""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from auto_coder.management.self_update_handoff import HandoffPhase, HandoffStore
from auto_coder.management.self_update_preflight import ManagementSelfUpdatePreflight
from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementReadyReceipt
from tests.test_management_self_update_integration import (
    INSTANCE,
    ManagementSystem,
    _effect_counts,
    _operation,
    _run,
    _snapshot_source,
    _test_source,
    _update,
    _wait,
    _wait_operation,
    management_system,
)

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]

# Importing the fixture registers the shared real production-origin environment
# for this companion module; all effects still enter through authenticated HTTP.
management_system


def _stage_revision(system: ManagementSystem, name: str) -> ManagementReadyReceipt:
    sha = _snapshot_source(system.source, name)
    _test_source(system.services, sha, 13)
    return system.stager.prepare(ManagementPrepareIntent(sha, name)).receipt


@pytest.mark.parametrize("boundary", ["preflight", "committed-handoff"])
def test_authenticated_stale_child_lifetime_cannot_switch_or_release_uncertain_lane(management_system: ManagementSystem, boundary: str) -> None:
    system = management_system
    token = system.issue()
    old = system.child
    child_database = system.root / "supervisor" / "management-child.sqlite3"
    handoff_database = system.root / "supervisor" / "management-self-update.sqlite3"
    handoffs = HandoffStore(system.root / "supervisor", INSTANCE)
    key = "stale-child-" + boundary
    stale_token = str(int(old.start_token) + 1)

    def physical_effects():
        with sqlite3.connect(child_database) as connection:
            return connection.execute("SELECT * FROM effects ORDER BY operation_id").fetchall()

    effects = physical_effects()
    # A kernel pidfd stays attached to this exact lifetime, independently of the
    # corrupted ledger and the production /proc start-token implementation.
    pidfd = os.pidfd_open(old.pid)
    lifetime = select.poll()
    lifetime.register(pidfd, select.POLLIN)
    command = Path(f"/proc/{old.pid}/cmdline").read_bytes()
    assert str(system.config_path).encode() in command.split(b"\0")
    assert Path(f"/proc/{old.pid}/exe").resolve() == Path(system.current.interpreter_path).resolve()
    submitted = None
    try:
        if boundary == "preflight":
            # The real CI lookup occurs after forward captured the original
            # running child and before its final post-preflight lifetime check.
            system.services.hold_next_ci = True
        else:
            # Keep the stop claim blocked until the consumer rejects the stale
            # evidence, including any in-flight observation made before injection.
            with sqlite3.connect(handoff_database) as connection:
                connection.execute("CREATE TRIGGER hold_stale_stop BEFORE UPDATE OF phase ON handoffs WHEN NEW.phase='stopping' BEGIN SELECT RAISE(ABORT, 'controlled stale-lifetime boundary'); END")

        accepted = _update(system, token, key)
        assert accepted["status"] == "ACCEPTED", accepted
        operation_id = accepted["operation_id"]
        assert isinstance(operation_id, str)
        if boundary == "preflight":
            assert system.services.held_ci.wait(20), "forward did not reach the actual CI HTTP boundary"
        else:
            submitted = _wait(lambda: handoffs.read(operation_id), timeout=90, description="real committed handoff before stop")
            _wait(lambda: "reconciliation unavailable operation=" + operation_id in (system.root / "supervisor.log").read_text(), description="blocked physical-stop claim")
            assert submitted.phase is HandoffPhase.SUBMITTED
            assert submitted.handoff.expected_child == old
        original = system.journal.get(operation_id).record
        assert original is not None
        assert original.effect.state.value == ("not_sent" if boundary == "preflight" else "sent_unknown")
        assert lifetime.poll(0) == []
        assert physical_effects() == effects

        # Change only retained lifetime evidence. The real installed child,
        # immutable original handoff, original authority and HTTP service survive.
        with sqlite3.connect(child_database) as connection:
            encoded = connection.execute("SELECT record_json FROM child_state WHERE instance_id=?", (INSTANCE,)).fetchone()[0]
            child = json.loads(encoded)
            assert (child["pid"], child["start_token"]) == (old.pid, old.start_token)
            child["start_token"] = stale_token
            assert connection.execute("UPDATE child_state SET record_json=? WHERE instance_id=? AND record_json=?", (json.dumps(child, sort_keys=True), INSTANCE, encoded)).rowcount == 1
        system.services.release_ci.set()

        if boundary == "preflight":
            observed = _wait_operation(system, token, operation_id, {"NOT_READY"})
            assert observed["safe_reason"] == "NOT_READY:management_lifetime_changed"
            assert observed["disposition"] == "NOT_STARTED"
            assert observed["effect_state"] == "rejected"
            assert handoffs.read(operation_id) is None
        else:
            observed = _wait_operation(system, token, operation_id, {"NEEDS_ATTENTION"})
            assert observed["safe_reason"] == "NEEDS_ATTENTION:expected management child lifetime changed before handoff"
            assert observed["effect_state"] == "sent_unknown"
            stopped = handoffs.read(operation_id)
            assert stopped is not None and stopped.phase is HandoffPhase.NEEDS_ATTENTION
            assert stopped.handoff == submitted.handoff
            assert stopped.proof is None and not stopped.selected_identity
            with sqlite3.connect(handoff_database) as connection:
                connection.execute("DROP TRIGGER hold_stale_stop")

        retained = system.journal.get(operation_id).record
        assert retained is not None
        assert retained.authority == original.authority
        assert retained.intent == original.intent
        # The refusal/attention phase has no active dispatcher owner. The
        # durable physical attempt below must still retain its original fence.
        assert retained.owner_attempt_id == ""
        assert (retained.effect.attempt_id, retained.effect.owner_generation) == (original.effect.attempt_id, original.effect.owner_generation)
        if boundary == "preflight":
            # The durable refusal adds its own observation correlation without
            # inventing a physical attempt or an effect generation.
            assert retained.effect.correlation_identity == "not-started:" + operation_id
        else:
            assert retained.effect.correlation_identity == original.effect.correlation_identity
        for _ in range(2):
            fresh = system.issue()
            assert system.call(fresh, "auto_coder_whoami")["uid"] == "operator"
            readback = _operation(system, fresh, operation_id)
            replay = _update(system, fresh, key)
            assert readback["status"] == replay["status"] == observed["status"]
            assert readback["operation_id"] == replay["operation_id"] == operation_id
            assert readback["status"] != "SUCCEEDED"
            if boundary == "committed-handoff":
                assert _update(system, fresh, "different-stale-child-intent")["status"] == "BUSY"
                assert handoffs.read(operation_id).handoff == submitted.handoff
                assert handoffs.read(operation_id).phase is HandoffPhase.NEEDS_ATTENTION
            replayed = system.journal.get(operation_id).record
            assert replayed is not None and replayed.authority == retained.authority
            assert replayed.effect == retained.effect
            assert replayed.intent == retained.intent
            assert len(system.journal.list_operations().records) == 1
            assert lifetime.poll(0) == []
            assert Path(f"/proc/{old.pid}/cmdline").read_bytes() == command
            assert Path(f"/proc/{old.pid}/exe").resolve() == Path(system.current.interpreter_path).resolve()
            assert physical_effects() == effects
            assert system.child.pid == old.pid and not system.child.intended_stop
            assert system.child.start_token == stale_token
            assert system.child.selected_runtime == system.current.management_runtime_id
            assert system.supervisor is not None and system.supervisor.poll() is None
    finally:
        system.services.release_ci.set()
        if system.supervisor is not None and system.supervisor.poll() is None:
            system.supervisor.terminate()
            system.supervisor.wait(timeout=10)
        # Restore only the injected token for fixture teardown. No assertion or
        # operation retry is allowed to depend on this test-only repair.
        try:
            with sqlite3.connect(child_database) as connection:
                row = connection.execute("SELECT record_json FROM child_state WHERE instance_id=?", (INSTANCE,)).fetchone()
                if row is not None:
                    child = json.loads(row[0])
                    if (child["pid"], child["start_token"]) == (old.pid, stale_token):
                        child["start_token"] = old.start_token
                        connection.execute("UPDATE child_state SET record_json=? WHERE instance_id=? AND record_json=?", (json.dumps(child, sort_keys=True), INSTANCE, row[0]))
            with sqlite3.connect(handoff_database) as connection:
                connection.execute("DROP TRIGGER IF EXISTS hold_stale_stop")
        finally:
            # A failed assertion or corrupted ledger cannot leak the actual
            # original child: signal its kernel lifetime, never a reused PID.
            try:
                if not lifetime.poll(0):
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                    _wait(lambda: lifetime.poll(0), timeout=12, description="stale-evidence fixture child exit")
            finally:
                os.close(pidfd)


def test_installed_incompatible_oauth_schema_refuses_authenticated_update_before_stop(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue()
    before_child = system.child
    before_effects = _effect_counts(system)
    before_oauth = ManagementSelfUpdatePreflight._snapshot(system.settings.oauth.database_path)
    before_configuration = system.bootstrap_path.read_bytes()
    candidate_source = system.source / "src" / "auto_coder" / "management" / "oauth_store.py"
    source = candidate_source.read_text(encoding="utf-8")
    needle = "            connection.close()\n        except (sqlite3.Error, OSError):"
    assert source.count(needle) == 1
    candidate_source.write_text(
        source.replace(needle, '            connection.execute("ALTER TABLE grants RENAME COLUMN uid TO incompatible_uid")\n' + needle),
        encoding="utf-8",
    )
    incompatible = _stage_revision(system, "incompatible-oauth-schema")
    assert incompatible.source_sha not in {system.current_sha, system.candidate_sha}

    accepted = _update(system, token, "incompatible-schema", source=incompatible.source_sha)
    assert accepted["status"] == "ACCEPTED", accepted
    operation_id = accepted["operation_id"]
    assert isinstance(operation_id, str)
    rejected = _wait_operation(system, token, operation_id, {"NOT_READY"})

    assert rejected["status"] == "NOT_READY", rejected
    assert rejected["disposition"] == "NOT_STARTED"
    assert "candidate_incompatible" in str(rejected["safe_reason"])
    assert system.child == before_child
    assert _effect_counts(system) == before_effects
    assert ManagementSelfUpdatePreflight._snapshot(system.settings.oauth.database_path) == before_oauth
    assert system.bootstrap_path.read_bytes() == before_configuration
    assert system.call(token, "auto_coder_whoami")["uid"] == "operator"
    assert rejected["operation_id"] == operation_id
    retained = system.journal.get(operation_id).record
    assert retained is not None and retained.effect.state.value == "rejected"
    assert retained.intent.target.channel == "management"
    assert _operation(system, token, operation_id)["status"] == "NOT_READY"
    assert _update(system, token, "incompatible-schema", source=incompatible.source_sha)["operation_id"] == operation_id
    assert len(system.journal.list_operations().records) == 1
    assert system.child == before_child
    assert _effect_counts(system) == before_effects


@pytest.mark.parametrize("management_system", ["recovery_installed"], indirect=True)
def test_real_installed_candidate_startup_exception_restores_exact_previous_once(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue()
    old = system.child
    before_effects = dict(_effect_counts(system))
    before_oauth = ManagementSelfUpdatePreflight._snapshot(system.settings.oauth.database_path)
    bootstrap_bytes = system.bootstrap_path.read_bytes()
    candidate_entrypoint = system.source / "src" / "auto_coder" / "cli.py"
    source = candidate_entrypoint.read_text(encoding="utf-8")
    needle = "    uvicorn.run(application, host=settings.bind_host, port=settings.port, access_log=False)"
    assert source.count(needle) == 1
    # This tested revision's real serving entrypoint fails after application
    # construction. --help and private compatibility probes still execute, so
    # only an actual replacement process can expose this startup failure.
    candidate_entrypoint.write_text(source.replace(needle, '    raise RuntimeError("candidate serving startup failed")\n' + needle), encoding="utf-8")
    candidate = _stage_revision(system, "failed-live-startup")
    assert candidate.source_sha not in {system.current_sha, system.candidate_sha}

    accepted = _update(system, token, "startup-failure", source=candidate.source_sha)
    operation_id = accepted["operation_id"]
    assert isinstance(operation_id, str)
    assert accepted["status"] == "ACCEPTED", accepted

    def previous_running():
        child = system.child
        return child if child.recovery_used and child.selected_runtime == system.current.management_runtime_id and child.child_generation == old.child_generation + 2 and child.pid not in {0, old.pid} else None

    restored_child = _wait(previous_running, timeout=120, description="one exact previous executable after candidate startup failure")
    system.wait_serving(runtime=system.current.management_runtime_id)
    assert system.call(token, "auto_coder_whoami")["uid"] == "operator"

    restored = _wait_operation(system, token, operation_id, {"PROCESS_RESTORED"})
    assert restored["operation_id"] == operation_id
    assert restored["phase"] == "rolled_back"
    assert ManagementSelfUpdatePreflight._snapshot(system.settings.oauth.database_path) == before_oauth
    assert system.bootstrap_path.read_bytes() == bootstrap_bytes
    assert system.child.pid == restored_child.pid
    assert system.child.source_receipt == system.current.receipt_identity
    assert system.child.runtime_receipt == "sha256:" + system.current.python_executable_sha256
    after_effects = dict(_effect_counts(system))
    assert after_effects["REQUEST_GRACEFUL_STOP"] == before_effects.get("REQUEST_GRACEFUL_STOP", 0) + 1
    assert after_effects["START_SELECTED"] == before_effects["START_SELECTED"] + 1
    assert after_effects.get("RESTORE_PREVIOUS", 0) == before_effects.get("RESTORE_PREVIOUS", 0)
    # The existing Supervisor records this automatic return on the exact start
    # effect; no extra restore command, crash loop, or new operation is needed.
    for _ in range(2):
        assert _operation(system, token, operation_id)["status"] == "PROCESS_RESTORED"
        replay = _update(system, token, "startup-failure", source=candidate.source_sha)
        assert replay["operation_id"] == operation_id and replay["status"] == "PROCESS_RESTORED"
    assert system.child.pid == restored_child.pid
    assert system.child.child_generation == old.child_generation + 2
    assert dict(_effect_counts(system)) == after_effects
    records = system.journal.list_operations().records
    assert len(records) == 1 and records[0].authority.trusted_grant_id
    receipt_json = system.root / "supervisor" / "management-self-update.sqlite3"
    assert receipt_json.exists()
    with sqlite3.connect(receipt_json) as connection:
        handoff = json.loads(connection.execute("SELECT record_json FROM handoffs WHERE operation_id=?", (operation_id,)).fetchone()[0])
    assert handoff["phase"] == "process_restored" and handoff["proof"] is None
    assert handoff["handoff"]["candidate"]["source_sha"] == candidate.source_sha
    assert handoff["handoff"]["previous"]["receipt_identity"] == system.current.receipt_identity

    # A proven self-update safety return is a completed episode. A later
    # separate child death may be recovered only after its handoff and journal
    # still join to the exact restored lifetime.
    from auto_coder.management.bootstrap_store import ChildPhase
    from auto_coder.management.recovery_mailbox import RecoveryMailbox

    os.kill(restored_child.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION and child.child_generation == restored_child.child_generation else None), timeout=30, description="restored child exit")
    mailbox = RecoveryMailbox(system.bootstrap.state_root, INSTANCE)

    def submit(request_id: str) -> None:
        output = _run(
            [
                sys.executable,
                "-m",
                "auto_coder.cli",
                "management",
                "recover",
                "--config",
                str(system.config_path),
                "--request-id",
                request_id,
                "--expected-supervisor",
                stopped.supervisor_incarnation,
                "--expected-generation",
                str(stopped.child_generation),
                "--expected-runtime",
                stopped.selected_runtime,
                "--expected-receipt",
                stopped.runtime_receipt,
                "--expected-source-receipt",
                stopped.source_receipt,
            ],
            system.root,
            timeout=20,
        )
        assert json.loads(output.splitlines()[-1])["status"] in {"pending", "claimed", "launched", "refused", "succeeded"}

    with sqlite3.connect(receipt_json) as connection:
        original = connection.execute("SELECT record_json,phase FROM handoffs WHERE operation_id=?", (operation_id,)).fetchone()
    assert original is not None
    for field, value in (("phase", "needs_attention"), ("restored_identity", "forged-lifetime")):
        altered = json.loads(original[0])
        altered[field] = value
        phase = value if field == "phase" else original[1]
        with sqlite3.connect(receipt_json) as connection:
            connection.execute("UPDATE handoffs SET record_json=?,phase=? WHERE operation_id=?", (json.dumps(altered, sort_keys=True), phase, operation_id))
        try:
            request_id = str(uuid.uuid4())
            submit(request_id)
            assert _wait(lambda: mailbox.get(request_id).status == "refused", timeout=30, description="unsettled or forged handoff refusal")
            assert dict(_effect_counts(system)) == after_effects
        finally:
            with sqlite3.connect(receipt_json) as connection:
                connection.execute("UPDATE handoffs SET record_json=?,phase=? WHERE operation_id=?", (*original, operation_id))
    request_id = str(uuid.uuid4())
    submit(request_id)
    launched = _wait(lambda: (item if (item := mailbox.get(request_id)).status == "launched" else None), timeout=40, description="local recovery after completed restore")
    system.wait_serving(runtime=system.current.management_runtime_id)
    latest = system.child
    assert latest.child_generation == restored_child.child_generation + 1
    assert latest.pid not in {0, old.pid, restored_child.pid}
    assert dict(_effect_counts(system))["RECOVER_SELECTED"] == 1
    assert system.call(token, "auto_coder_whoami", {"observation_id": launched.observation_id})["uid"] == "operator"
    assert _wait(lambda: mailbox.get(request_id).status == "succeeded", timeout=30, description="fresh authenticated recovery proof")
    submit(request_id)
    assert dict(_effect_counts(system))["RECOVER_SELECTED"] == 1
