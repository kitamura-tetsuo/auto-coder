"""Typed, redacted evidence projected from explicit PR processing."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import cast

from .pr_repair_dispatch import PRRepairResult, RepairFactState
from .security_utils import redact_data

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RepairEvidence:
    repository: str = ""
    pr_number: int = 0
    input_head: str = ""
    disposition: str = ""
    attempt_started: bool | None = None
    resulting_head_state: str = RepairFactState.UNKNOWN.value
    resulting_head: str = ""
    synchronization_state: str = RepairFactState.UNKNOWN.value
    synchronized: bool = False
    actions: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class FinalGateEvidence:
    disposition: str = ""
    repository: str = ""
    pr_number: int = 0
    evaluated_head: str = ""
    evaluated_base: str = ""


@dataclass(frozen=True)
class EditingInvocationEvidence:
    backend_alias: str = ""
    backend_type: str = ""
    model: str = ""
    purpose: str = ""
    provider: str = ""
    invocation_id: str = ""
    continuation: bool = False
    status: str = "started"


_invocations: ContextVar[tuple[EditingInvocationEvidence, ...]] = ContextVar("pr_processing_invocations", default=())
_target: ContextVar[tuple[str, int] | None] = ContextVar("pr_processing_target", default=None)
_purpose: ContextVar[tuple[str, int, str] | None] = ContextVar("pr_repair_purpose", default=None)


def begin_invocation_observation() -> None:
    _invocations.set(())


@contextmanager
def observe_pr_processing(repository: str, target_number: int):
    target_token = _target.set((repository, target_number))
    _invocations.set(())
    try:
        yield
    finally:
        _target.reset(target_token)


@contextmanager
def bind_pr_repair_purpose(repository: str, pr_number: int, purpose: str):
    token = _purpose.set((repository, pr_number, purpose))
    try:
        yield
    finally:
        _purpose.reset(token)


def observe_editing_dispatch(
    *,
    backend: str,
    backend_type: str,
    provider: str | None,
    model: str | None,
    invocation_id: str,
    continuation: bool,
    is_noedit: bool,
    status: str,
) -> None:
    target = _target.get()
    purpose = _purpose.get()
    if is_noedit or target is None or purpose is None or target[:2] != purpose[:2]:
        return
    current = _invocations.get()
    evidence = EditingInvocationEvidence(
        backend_alias=backend,
        backend_type=backend_type,
        model=model or "",
        purpose=purpose[2],
        provider=provider or "",
        invocation_id=invocation_id,
        continuation=continuation,
        status=status,
    )
    if status == "started":
        _invocations.set((*current, evidence))
    elif current and current[-1].invocation_id == invocation_id:
        _invocations.set((*current[:-1], evidence))


def observed_invocations() -> tuple[EditingInvocationEvidence, ...]:
    return _invocations.get()


def project_repair(result: PRRepairResult | None) -> RepairEvidence | None:
    if result is None:
        return None
    return RepairEvidence(
        repository=result.repository,
        pr_number=result.pr_number,
        input_head=result.input_head,
        disposition=result.disposition.value,
        attempt_started=result.attempt_started,
        resulting_head_state=result.resulting_head_state.value,
        resulting_head=result.resulting_head,
        synchronization_state=result.synchronization_state.value,
        synchronized=result.synchronized,
        actions=result.actions,
        reason=result.reason,
    )


def redacted_dict(value: RepairEvidence | FinalGateEvidence | EditingInvocationEvidence | None) -> dict[str, object] | None:
    if value is None:
        return None
    raw = asdict(value)
    raw["actions"] = list(raw.get("actions", [])) if "actions" in raw else raw.get("actions")
    return cast(dict[str, object], redact_data(raw))
