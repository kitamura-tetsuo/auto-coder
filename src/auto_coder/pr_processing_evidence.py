"""Typed, redacted evidence projected from explicit PR processing."""

from __future__ import annotations

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


_invocations: ContextVar[tuple[EditingInvocationEvidence, ...]] = ContextVar("pr_processing_invocations", default=())


def begin_invocation_observation() -> None:
    _invocations.set(())


def record_editing_invocation(manager: object | None, purpose: str, repository: str) -> None:
    if manager is None:
        return
    getter = getattr(manager, "get_last_backend_and_model", None)
    if not callable(getter):
        return
    backend, model = getter()
    if not backend:
        return
    from .llm_backend_config import get_llm_config

    config = get_llm_config(repo_name=repository)
    try:
        backend_type = config.resolve_backend_type(str(backend))
    except ValueError:
        backend_type = "unresolved"
    current = _invocations.get()
    _invocations.set((*current, EditingInvocationEvidence(str(backend), backend_type, str(model or ""), purpose)))


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
