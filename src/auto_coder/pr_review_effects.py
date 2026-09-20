"""Durable external-effect boundary for accepted two-tier review results.

The review-cycle repository decides what evidence is accepted.  This module
does the deliberately separate job of reserving a non-idempotent external
mutation, recording its receipt, and reconciling an indeterminate response.
It contains no reviewer/model invocation and never treats prose as authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol

from .pr_review_cycle import ClosureCertification, Finding, StrongAuditRound
from .runtime_locks import ensure_lock_directory, lock_path

RESERVED = "RESERVED"
UNCERTAIN = "UNCERTAIN"
CONFIRMED = "CONFIRMED"
REJECTED = "REJECTED"
RETIRED = "RETIRED"


def _identity(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class AcceptedReviewPayload:
    """The exact portable payload consumed by an external review effect."""

    repository: str
    pr_number: int
    open_epoch: int
    mode: str
    round_id: str
    attempt: int
    audited_head: str
    target_head: str
    base_sha: str
    contract_identity: str
    contract_issue_ids: tuple[str, ...]
    requirements_text: str
    policy_identity: str
    policy_route: str
    policy_options: str
    policy_protocol: str
    finding_set_revision: int
    reviewer_provenance: str
    verdict: str
    findings: tuple[Finding, ...]
    bounded_evidence: str = ""

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @property
    def identity(self) -> str:
        return _identity("accepted-review-payload-v1", self.canonical_json())

    @classmethod
    def strong(
        cls,
        repository: str,
        pr_number: int,
        record: StrongAuditRound,
        findings: tuple[Finding, ...],
    ) -> "AcceptedReviewPayload":
        selected = tuple(item for item in findings if item.origin_round_id == record.claim_id)
        if {item.finding_id for item in selected} != set(record.finding_ids):
            raise ValueError("Strong publication requires the complete accepted finding bundle")
        return cls(
            repository,
            pr_number,
            record.open_epoch,
            "STRONG_AUDIT",
            record.round_id,
            record.sequence,
            record.head_sha,
            record.head_sha,
            record.base_sha,
            record.contract_identity,
            record.contract_snapshot.issue_ids,
            record.contract_snapshot.requirements_text,
            record.policy_identity,
            record.policy.strong_route,
            record.policy.model_options,
            record.policy.protocol_version,
            record.finding_set_revision,
            record.reviewer_provenance,
            record.verdict,
            selected,
        )

    @classmethod
    def closure(
        cls,
        repository: str,
        pr_number: int,
        record: StrongAuditRound,
        closure: ClosureCertification,
        findings: tuple[Finding, ...],
    ) -> "AcceptedReviewPayload":
        return cls(
            repository,
            pr_number,
            record.open_epoch,
            "ORDINARY_CLOSURE",
            closure.closure_id,
            record.sequence,
            record.head_sha,
            closure.head_sha,
            closure.base_sha,
            closure.contract_identity,
            record.contract_snapshot.issue_ids,
            record.contract_snapshot.requirements_text,
            closure.policy_identity,
            record.policy.strong_route,
            record.policy.model_options,
            record.policy.protocol_version,
            closure.finding_set_revision,
            record.reviewer_provenance,
            "CLOSURE",
            findings,
            closure.bounded_evidence,
        )


@dataclass(frozen=True)
class EffectOperation:
    operation_id: str
    payload_identity: str
    repository: str
    pr_number: int
    mode: str
    round_id: str
    target_head: str
    contract_identity: str
    finding_set_revision: int
    purpose: str
    destination: str
    exact_payload: str
    status: str
    owner_token: str
    receipt: str = ""
    reason: str = ""
    updated_at: float = 0.0


@dataclass(frozen=True)
class EffectAttempt:
    """Transport result; uncertainty is intentionally not represented as failure."""

    status: str
    receipt: str = ""
    reason: str = ""


class EffectTransport(Protocol):
    def send(self, operation: EffectOperation) -> EffectAttempt: ...

    def reconcile(self, operation: EffectOperation) -> EffectAttempt: ...


class ReviewEffectRepository:
    """Atomic reservation/receipt journal shared by overlapping controllers."""

    def __init__(self, repo_name: str, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or Path.home() / ".auto-coder" / repo_name / "pr_review_effects.json"
        self.lock_path = lock_path(repo_name, self.storage_path, "pr-review-effects")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_lock_directory(self.lock_path)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.storage_path.exists():
            return {"operations": {}}
        loaded = json.loads(self.storage_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("operations"), dict):
            raise ValueError("review-effect state is invalid")
        return loaded

    def _write(self, state: dict) -> None:
        temporary = self.storage_path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.storage_path)

    @staticmethod
    def operation_identity(payload: AcceptedReviewPayload, purpose: str, destination: str) -> str:
        return _identity(
            "review-effect-v1",
            payload.repository,
            str(payload.pr_number),
            str(payload.open_epoch),
            payload.mode,
            payload.round_id,
            payload.target_head,
            payload.contract_identity,
            str(payload.finding_set_revision),
            payload.identity,
            purpose,
            destination,
        )

    @staticmethod
    def _from_raw(raw: dict) -> EffectOperation:
        return EffectOperation(**raw)

    def reserve(self, payload: AcceptedReviewPayload, purpose: str, destination: str, owner_token: str) -> EffectOperation:
        operation_id = self.operation_identity(payload, purpose, destination)
        with self._locked():
            state = self._read()
            operations = state["operations"]
            existing = operations.get(operation_id)
            if isinstance(existing, dict):
                return self._from_raw(existing)
            operation = EffectOperation(
                operation_id,
                payload.identity,
                payload.repository,
                payload.pr_number,
                payload.mode,
                payload.round_id,
                payload.target_head,
                payload.contract_identity,
                payload.finding_set_revision,
                purpose,
                destination,
                payload.canonical_json(),
                RESERVED,
                owner_token,
                updated_at=time.time(),
            )
            operations[operation_id] = asdict(operation)
            self._write(state)
            return operation

    def get(self, operation_id: str) -> Optional[EffectOperation]:
        with self._locked():
            raw = self._read()["operations"].get(operation_id)
            return self._from_raw(raw) if isinstance(raw, dict) else None

    def record(self, operation_id: str, owner_token: str, outcome: EffectAttempt) -> EffectOperation:
        if outcome.status not in {CONFIRMED, UNCERTAIN, REJECTED, RETIRED}:
            raise ValueError(f"invalid effect outcome: {outcome.status}")
        with self._locked():
            state = self._read()
            raw = state["operations"].get(operation_id)
            if not isinstance(raw, dict):
                raise KeyError(operation_id)
            current = self._from_raw(raw)
            if current.owner_token != owner_token:
                raise PermissionError("only the reservation owner may record its external outcome")
            if current.status in {CONFIRMED, RETIRED}:
                return current
            raw.update(status=outcome.status, receipt=outcome.receipt, reason=outcome.reason, updated_at=time.time())
            self._write(state)
            return self._from_raw(raw)


class ReviewEffectExecutor:
    """Execute or reconcile one reserved effect without blind replay."""

    def __init__(self, repository: ReviewEffectRepository, owner_token: Optional[str] = None):
        self.repository = repository
        self.owner_token = owner_token or uuid.uuid4().hex

    def apply(
        self,
        payload: AcceptedReviewPayload,
        purpose: str,
        destination: str,
        transport: EffectTransport,
        is_current: Callable[[], bool],
    ) -> EffectOperation:
        operation = self.repository.reserve(payload, purpose, destination, self.owner_token)
        if operation.status in {CONFIRMED, RETIRED}:
            return operation
        if operation.owner_token != self.owner_token:
            return operation
        if not is_current():
            return self.repository.record(operation.operation_id, self.owner_token, EffectAttempt(REJECTED, reason="accepted result or destination authority is no longer current"))
        # RESERVED means no send outcome was observed.  An UNCERTAIN operation
        # must be reconciled first and is never blindly sent again.
        outcome = transport.reconcile(operation) if operation.status == UNCERTAIN else transport.send(operation)
        return self.repository.record(operation.operation_id, self.owner_token, outcome)
