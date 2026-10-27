"""Authenticated self-update refusals and recovery with actual installed failures."""

from __future__ import annotations

import json
import sqlite3

import pytest

from auto_coder.management.self_update_preflight import ManagementSelfUpdatePreflight
from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementReadyReceipt
from tests.test_management_self_update_integration import (
    ManagementSystem,
    _effect_counts,
    _operation,
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
