"""Fenced automatic restart after an explicitly verified clean container stop."""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack
from dataclasses import asdict, replace

from .bootstrap_store import ChildPhase
from .bootstrap_supervisor import ManagementBootstrapSupervisor, ResultStatus
from .recovery_evidence import assert_current_owner, assert_journal_clear, assert_kernel_absence, read_child_evidence
from .recovery_mailbox import RecoveryMailbox
from .recovery_supervisor import RecoveryConsumer


def restart_verified_stop(supervisor: ManagementBootstrapSupervisor) -> ResultStatus:
    """Make one launch claim only for the exact confirmed prior clean stop."""
    config = supervisor.config
    try:
        with supervisor._lock, ExitStack() as stack:
            for path in (config.journal_root / "management-operations.sqlite3", config.state_root / "management-self-update.sqlite3", config.state_root / "management-recovery.sqlite3"):
                connection = RecoveryConsumer._locked(path)
                stack.callback(connection.close)
            assert_current_owner(config, supervisor.incarnation)
            mailbox = RecoveryMailbox(config.state_root, config.instance_id)
            reader = mailbox.connect(readonly=True)
            stack.callback(reader.close)
            for row in reader.execute("SELECT * FROM recovery_requests"):
                if mailbox._record(row).status in {"pending", "claimed", "launched", "unknown"}:
                    return ResultStatus.UNKNOWN
            assert_journal_clear(config)
            child, previous = read_child_evidence(config)
            expected_stop = "container-stop-" + child.supervisor_incarnation
            if child.phase is not ChildPhase.STOPPED or child.pid != 0 or not child.intended_stop or child.exit_cause != "intended_stop" or child.effect_operation_id != expected_stop or child.supervisor_incarnation == supervisor.incarnation:
                return ResultStatus.UNKNOWN
            with sqlite3.connect((config.state_root / "management-child.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
                rows = connection.execute("SELECT action,phase,result_json,expected_identity,runtime_ref FROM effects WHERE operation_id=? AND attempt_id=?", (child.effect_operation_id, child.effect_attempt_id)).fetchall()
            if len(rows) != 1 or rows[0][0:2] != ("REQUEST_GRACEFUL_STOP", "observed"):
                return ResultStatus.UNKNOWN
            prior = replace(child, pid=previous.pid, start_token=previous.start_token)
            expected_record = asdict(child)
            expected_record["phase"] = child.phase.value
            result = json.loads(rows[0][2])
            if rows[0][3] != supervisor._identity(prior) or rows[0][4] != "" or result.get("status") != ResultStatus.STOPPED.value or result.get("identity") != supervisor._identity(child) or result.get("record") != expected_record:
                return ResultStatus.UNKNOWN
            runtime = supervisor._runtime(child.selected_runtime)
            if runtime is None or (runtime.receipt, runtime.source_receipt) != (child.runtime_receipt, child.source_receipt) or not runtime.verify(initial=runtime.reference == config.initial_runtime):
                return ResultStatus.UNKNOWN
            if not supervisor._drain_acknowledged(runtime, prior):
                return ResultStatus.UNKNOWN
            assert_kernel_absence(config, previous)
            assert_current_owner(config, supervisor.incarnation)
            operation = "container-autostart-" + str(child.child_generation) + "-" + child.supervisor_incarnation
            return supervisor.start_selected(operation, operation, child.selected_runtime, supervisor._identity(child)).status
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return ResultStatus.UNKNOWN
