"""Bounded reconstruction of durable Codex producer journals."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from .codex_work_accounting import CodexWorkOperation, CodexWorkPhase, ReconstructionReceipt

REQUIRED_CODEX_SOURCES = (
    "cloud-runs",
    "cloud-bindings",
    "retry-authorizations",
    "retry-dispatch",
    "follow-ups",
    "repair-admissions",
    "pr-recovery",
)


@dataclass(frozen=True)
class CodexSourceSnapshot:
    """One atomic source enumeration and its store-provided consistency token."""

    source: str
    consistency_id: str
    operations: tuple[CodexWorkOperation, ...] = ()


class CodexWorkReconstructor:
    """Create receipts only from two identical complete source observations."""

    def __init__(self, readers: Mapping[str, Callable[[], CodexSourceSnapshot]]) -> None:
        self.readers = dict(readers)

    def consistency_ids(self, sources: tuple[str, ...] = REQUIRED_CODEX_SOURCES) -> Mapping[str, str]:
        snapshots = self._read(sources)
        return {snapshot.source: snapshot.consistency_id for snapshot in snapshots}

    def reconstruct(self) -> ReconstructionReceipt:
        missing = set(REQUIRED_CODEX_SOURCES) - set(self.readers)
        extra = set(self.readers) - set(REQUIRED_CODEX_SOURCES)
        if missing or extra:
            raise ValueError(f"Codex reconstruction sources mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
        first = self._read(REQUIRED_CODEX_SOURCES)
        second = self._read(REQUIRED_CODEX_SOURCES)
        first_ids = {item.source: item.consistency_id for item in first}
        second_ids = {item.source: item.consistency_id for item in second}
        if first_ids != second_ids:
            raise RuntimeError("Codex source changed during reconstruction")
        operations = tuple(operation for item in second for operation in item.operations)
        operation_ids = [operation.logical_operation_id for operation in operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("Codex source enumeration contains conflicting logical operations")
        manifest = tuple((item.source, tuple(operation.logical_operation_id for operation in item.operations)) for item in second)
        identity = hashlib.sha256(json.dumps({"sources": second_ids, "operations": operation_ids}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return ReconstructionReceipt(
            receipt_id=f"reconstruction:{identity}:{uuid.uuid4().hex}",
            sources=REQUIRED_CODEX_SOURCES,
            consistent_sources=tuple(item.source for item in second),
            source_operation_ids=manifest,
            operations=operations,
            source_consistency_ids=tuple(sorted(second_ids.items())),
        )

    def _read(self, sources: Iterable[str]) -> tuple[CodexSourceSnapshot, ...]:
        result: list[CodexSourceSnapshot] = []
        for source in sources:
            snapshot = self.readers[source]()
            if snapshot.source != source or not snapshot.consistency_id:
                raise ValueError(f"Invalid Codex source snapshot for {source}")
            result.append(snapshot)
        return tuple(result)


def _path_identity(path: object) -> str:
    from pathlib import Path

    value = Path(path)  # type: ignore[arg-type]
    digest = hashlib.sha256()
    found = False
    for candidate in (value, Path(f"{value}-wal")):
        try:
            payload = candidate.read_bytes()
        except FileNotFoundError:
            continue
        found = True
        digest.update(candidate.name.encode("utf-8"))
        digest.update(payload)
    return digest.hexdigest() if found else "absent"


def _value_identity(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def production_codex_reconstructor(repository: str, issue_number: int) -> CodexWorkReconstructor:
    """Bind reconstruction to the real durable producer stores for one Issue."""
    from pathlib import Path

    from .cloud_manager import CloudManager
    from .cloud_run import CloudRunRepository
    from .codex_cloud_client import PendingCodexFollowUp, _codex_followup_state_path, _load_pending_followups
    from .codex_pr_recovery import CodexPRRecoveryStore
    from .codex_work_fence import stable_codex_operation_id
    from .provider_repair_correlation import default_correlation_db_path
    from .retry_dispatch import RetryDispatchRepository

    runs = CloudRunRepository(repository)
    manager = CloudManager(repository)
    retries = RetryDispatchRepository(repository)
    followup_path = _codex_followup_state_path(repository)
    recovery = CodexPRRecoveryStore()
    routing_path = Path(os.environ.get("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", "~/.auto-coder/issue-stage-routing.sqlite3")).expanduser()
    repair_path = default_correlation_db_path()

    def operation(
        operation_id: str,
        kind: str,
        request: str,
        phase: CodexWorkPhase,
        task: str = "",
        baseline: str = "",
        *,
        publication_complete: bool = False,
        tracking_complete: bool = False,
    ) -> CodexWorkOperation:
        return CodexWorkOperation(
            operation_id,
            kind,
            request,
            baseline or None,
            task or None,
            phase,
            False,
            publication_complete,
            tracking_complete,
            None,
            phase is CodexWorkPhase.ACCEPTED,
        )

    def cloud_runs() -> CodexSourceSnapshot:
        selected = [run for run in runs.list_all() if run.issue_number == issue_number and run.provider == "codex-cloud"]
        operations = []
        for run in selected:
            request = run.launch_identity or f"{repository}#{issue_number}:attempt:{run.attempt}"
            phase = CodexWorkPhase.ACCEPTED if run.task_id and run.submission_outcome == "accepted" else (CodexWorkPhase.DELIVERY_UNKNOWN if run.submission_outcome == "indeterminate" else CodexWorkPhase.RESERVED)
            recovery_record = recovery.get(repository, run.task_id) if run.task_id else None
            handoff_complete = bool(recovery_record and recovery_record.pr_number and recovery_record.handoff_complete)
            operations.append(
                operation(
                    stable_codex_operation_id("submission", request),
                    "submission",
                    request,
                    phase,
                    run.task_id,
                    publication_complete=handoff_complete,
                    tracking_complete=handoff_complete,
                )
            )
        return CodexSourceSnapshot("cloud-runs", _path_identity(runs.storage_path), tuple(operations))

    def bindings() -> CodexSourceSnapshot:
        manager.read_bindings_strict()
        return CodexSourceSnapshot("cloud-bindings", _path_identity(manager.cloud_file_path))

    def retry_authorizations() -> CodexSourceSnapshot:
        rows: list[tuple[object, ...]] = []
        if routing_path.exists():
            with sqlite3.connect(routing_path) as connection:
                rows = connection.execute(
                    "SELECT request_id,generation,attempt_id,status,ownership_reference,predecessor_provider,predecessor_task_id FROM implementation_retry_requests WHERE repository=? AND target_number=? ORDER BY request_id",
                    (repository, issue_number),
                ).fetchall()
        return CodexSourceSnapshot("retry-authorizations", _value_identity(rows))

    def retry_dispatch() -> CodexSourceSnapshot:
        operations = []
        handoffs = retries.list_for_issue(issue_number)
        for handoff in handoffs:
            if handoff.route != "codex-cloud" or handoff.outcome == "definitely-not-started":
                continue
            phase = CodexWorkPhase.ACCEPTED if handoff.outcome in {"accepted", "completed"} else (CodexWorkPhase.DELIVERY_UNKNOWN if handoff.outcome == "indeterminate" else CodexWorkPhase.RESERVED)
            operations.append(operation(stable_codex_operation_id("retry", handoff.request_id), "retry", handoff.request_id, phase, handoff.external_id or ""))
        return CodexSourceSnapshot("retry-dispatch", _value_identity(handoffs), tuple(operations))

    def followups() -> CodexSourceSnapshot:
        task_ids = {run.task_id for run in runs.list_all() if run.issue_number == issue_number and run.provider == "codex-cloud" and run.task_id}
        records = _load_pending_followups(followup_path)
        grouped: dict[tuple[str, str, str], list[tuple[str, PendingCodexFollowUp]]] = {}
        for key, record in records.items():
            if record.task_id not in task_ids:
                continue
            grouped.setdefault((record.task_id, record.message_fingerprint, record.pre_send_turn_id), []).append((key, record))
        operations = []
        for (task, _fingerprint, baseline), members in grouped.items():
            identities = sorted(record.logical_identity or key for key, record in members)
            statuses = {record.status for _key, record in members}
            phase = CodexWorkPhase.ACCEPTED if statuses == {"delivered"} else CodexWorkPhase.DELIVERY_UNKNOWN
            request = ",".join(identities)
            operations.append(operation(stable_codex_operation_id("follow-up", request), "follow-up", request, phase, task, baseline))
        return CodexSourceSnapshot("follow-ups", _path_identity(followup_path), tuple(operations))

    def repairs() -> CodexSourceSnapshot:
        operations = []
        rows: list[tuple[object, ...]] = []
        if repair_path.exists():
            with sqlite3.connect(repair_path) as connection:
                rows = connection.execute("SELECT ticket_id,owner_id,baseline_token FROM admission_tickets WHERE repository=? AND provider='codex-cloud'", (repository,)).fetchall()
            task_ids = {run.task_id for run in runs.list_all() if run.issue_number == issue_number and run.task_id}
            for ticket, task, baseline in rows:
                if task in task_ids:
                    operations.append(operation(stable_codex_operation_id("repair", str(ticket)), "repair", str(ticket), CodexWorkPhase.RESERVED, str(task), str(baseline)))
        return CodexSourceSnapshot("repair-admissions", _value_identity(rows), tuple(operations))

    def pr_recovery() -> CodexSourceSnapshot:
        operations = []
        rows: list[tuple[object, ...]] = []
        task_ids = {run.task_id for run in runs.list_all() if run.issue_number == issue_number and run.task_id}
        if recovery.path.exists():
            with sqlite3.connect(recovery.path) as connection:
                rows = connection.execute(
                    "SELECT task_id,state,completion_turn,pr_number,handoff_complete FROM codex_pr_recovery WHERE repository=? AND provider='codex-cloud'",
                    (repository,),
                ).fetchall()
            for task, state, baseline, pr_number, handoff_complete in rows:
                if task in task_ids and state in {"reminder_reserved", "reminder_accepted", "delivery_indeterminate", "pr_observed"}:
                    phase = CodexWorkPhase.ACCEPTED if state in {"reminder_accepted", "pr_observed"} else (CodexWorkPhase.DELIVERY_UNKNOWN if state == "delivery_indeterminate" else CodexWorkPhase.RESERVED)
                    request = f"initial-pr-publication:v1:{baseline}"
                    completed = bool(pr_number and handoff_complete)
                    operations.append(
                        operation(
                            stable_codex_operation_id("publication", request),
                            "publication",
                            request,
                            phase,
                            str(task),
                            str(baseline or ""),
                            publication_complete=completed,
                            tracking_complete=completed,
                        )
                    )
        return CodexSourceSnapshot("pr-recovery", _value_identity(rows), tuple(operations))

    return CodexWorkReconstructor(
        {
            "cloud-runs": cloud_runs,
            "cloud-bindings": bindings,
            "retry-authorizations": retry_authorizations,
            "retry-dispatch": retry_dispatch,
            "follow-ups": followups,
            "repair-admissions": repairs,
            "pr-recovery": pr_recovery,
        }
    )


def reconstruct_active_codex_work(repository: str, slots: object) -> None:
    """Reconcile every active Issue reservation during daemon restart."""
    from .codex_work_accounting import CodexWorkAccounting
    from .implementation_slots import ImplementationOwner, ImplementationSlotRepository

    if not isinstance(slots, ImplementationSlotRepository):
        raise TypeError("Codex reconstruction requires the production slot repository")
    for owner in slots.active_owners():
        if owner.kind != "issue":
            continue
        incarnation = slots.owner_incarnation(ImplementationOwner("issue", owner.number))
        if not incarnation:
            raise RuntimeError(f"Active Issue #{owner.number} has no incarnation")
        reconstructor = production_codex_reconstructor(repository, owner.number)
        receipt = reconstructor.reconstruct()
        if not receipt.operations:
            continue
        accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
        accounting.reconcile_from_receipt(owner, incarnation, receipt)
