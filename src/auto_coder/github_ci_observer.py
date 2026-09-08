"""Phase-scoped GitHub Actions observation adapter.

The adapter is deliberately read-only.  Policy effects (notably deployment
approval) consume its immutable output at a separate boundary.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Iterator, TypeVar
from uuid import uuid4

from .ci_observation import (
    CheckExecutionIdentity,
    CheckObservation,
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from .logger_config import get_logger
from .util.github_request_outcome import GitHubRequestError

logger = get_logger(__name__)
T = TypeVar("T")
_PAGE_SIZE = 100
_ADVISORY_WORKFLOW_PATHS = frozenset(
    {
        ".github/workflows/prompt-regression.yml",
        ".github/workflows/prompt-regression-report.yml",
    }
)


def _conclusion(status: str, conclusion: object) -> CIConclusion:
    if status.lower() in {"queued", "pending", "in_progress", "waiting", "requested"}:
        return CIConclusion.PENDING
    value = str(conclusion or "unknown").lower()
    aliases = {"failure": CIConclusion.FAILURE, "failed": CIConclusion.FAILURE, "timed_out": CIConclusion.TIMED_OUT}
    try:
        return aliases.get(value, CIConclusion(value))
    except ValueError:
        return CIConclusion.UNKNOWN


@dataclass
class _Phase:
    identity: str = field(default_factory=lambda: str(uuid4()))
    epoch: int = 0
    cache: dict[tuple[str, str, int, str, str], CIObservationSnapshot] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


_local = threading.local()
_active_phases: list[_Phase] = []
_active_phases_lock = threading.Lock()
_approval_guard = threading.Lock()
_approval_locks: dict[tuple[str, int], threading.Lock] = {}
_confirmed_approvals: set[tuple[str, int, int, tuple[int, ...], str]] = set()
_indeterminate_approvals: set[tuple[str, int, int, tuple[int, ...], str]] = set()


@contextmanager
def ci_read_phase(reason: str = "read-only") -> Iterator[None]:
    """Share exact observations only for the lifetime of this read-only phase."""
    previous = getattr(_local, "phase", None)
    phase = _Phase()
    with _active_phases_lock:
        _active_phases.append(phase)
    _local.phase = phase
    logger.debug(f"CI observation phase={phase.identity} epoch=0 reason={reason} started")
    try:
        yield
    finally:
        phase.cache.clear()
        _local.phase = previous
        with _active_phases_lock:
            if phase in _active_phases:
                _active_phases.remove(phase)
        logger.debug(f"CI observation phase={phase.identity} ended")


def ci_read_phase_method(function: Callable[..., T]) -> Callable[..., T]:
    """Bind a controller method to one observation phase."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> T:
        with ci_read_phase(function.__name__):
            return function(*args, **kwargs)

    return wrapped


def end_ci_read_phase(reason: str) -> None:
    """Fence reuse before mutations, governor waits, or external work."""
    phase = getattr(_local, "phase", None)
    if phase is not None:
        with phase.lock:
            phase.epoch += 1
            phase.cache.clear()
            logger.debug(f"CI observation phase={phase.identity} epoch={phase.epoch} fenced reason={reason}")


def fence_active_ci_observations(reason: str) -> None:
    """Fence reads immediately when durable webhook evidence is accepted."""
    with _active_phases_lock:
        phases = tuple(_active_phases)
    for phase in phases:
        # Do not acquire the phase's network-read lock: webhook intake must not
        # wait for (and thereby depend on) an outbound GitHub request. The GIL
        # makes this epoch assignment atomic; observe_ci compares its captured
        # value before making the result available.
        phase.epoch += 1
        phase.cache.clear()
        logger.debug(f"CI observation phase={phase.identity} epoch={phase.epoch} fenced reason={reason}")


def _credential_identity(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def observe_ci(api: Any, token: str, repository: str, pr_number: int, head_sha: str, api_origin: str = "https://api.github.com") -> CIObservationSnapshot:
    """Read every targeted checks/runs page and return fail-closed facts."""
    phase = getattr(_local, "phase", None) or _Phase()
    owner, repo = repository.split("/", 1)
    credential = _credential_identity(token)
    key = (api_origin, repository, pr_number, head_sha, credential)
    subject = ObservationSubject(api_origin, repository, pr_number, head_sha)
    request = ObservationRequest("github-actions", f"checks+workflows;head={head_sha};per_page={_PAGE_SIZE};auth={credential}")
    with phase.lock:  # also coalesces overlapping identical reads
        captured_epoch = phase.epoch
        cached = phase.cache.get(key)
        if cached is not None:
            logger.debug(f"CI observation cycle={cached.cycle_id} epoch={cached.invalidation_epoch} repo={repository} pr={pr_number} head={head_sha} availability={cached.availability.value} reason=phase-reuse")
            return cached
        cycle = str(uuid4())
        facts: list[CheckObservation | WorkflowObservation] = []
        try:
            for source, operation, field_name in (
                ("checks", api.checks.list_for_ref, "check_runs"),  # type: ignore[attr-defined]
                ("workflows", api.actions.list_workflow_runs_for_repo, "workflow_runs"),  # type: ignore[attr-defined]
            ):
                page = 1
                while True:
                    kwargs = {"ref": head_sha} if source == "checks" else {"head_sha": head_sha}
                    payload = operation(owner, repo, per_page=_PAGE_SIZE, page=page, **kwargs)
                    if not isinstance(payload, dict) or not isinstance(payload.get(field_name), list):
                        raise ValueError(f"malformed {source} page {page}")
                    entries = payload[field_name]
                    for item in entries:
                        if not isinstance(item, dict) or item.get("head_sha") != head_sha:
                            raise ValueError(f"malformed or mismatched {source} identity")
                        if source == "workflows":
                            workflow_path = str(item.get("path") or "").split("@", 1)[0]
                            if workflow_path in _ADVISORY_WORKFLOW_PATHS:
                                continue
                            run_id, workflow_id, attempt = item.get("id"), item.get("workflow_id"), item.get("run_attempt")
                            if not run_id or not workflow_id or not isinstance(attempt, int) or attempt <= 0:
                                raise ValueError("workflow run lacks id/workflow_id/run_attempt")
                            facts.append(WorkflowObservation(WorkflowExecutionIdentity(str(workflow_id), str(run_id), attempt), _conclusion(str(item.get("status") or ""), item.get("conclusion")), str(item.get("name") or ""), str(item.get("status") or "").lower() == "waiting"))
                        else:
                            check_id = item.get("id")
                            app = item.get("app")
                            app_id = app.get("id") if isinstance(app, dict) else None
                            if not check_id or not app_id:
                                raise ValueError("check run lacks check/app identity")
                            facts.append(CheckObservation(CheckExecutionIdentity(str(app_id), str(check_id)), _conclusion(str(item.get("status") or ""), item.get("conclusion")), str(item.get("name") or "")))
                    if len(entries) < _PAGE_SIZE:
                        break
                    page += 1
            availability = ObservationAvailability.KNOWN if facts else ObservationAvailability.KNOWN_EMPTY
            snapshot = CIObservationSnapshot(subject, request, cycle, captured_epoch, availability, tuple(facts))
        except GitHubRequestError as exc:
            classification = getattr(getattr(exc, "outcome", None), "classification", None)
            availability = ObservationAvailability.THROTTLED if str(getattr(classification, "value", classification)) in {"throttled", "forbidden", "authentication"} else ObservationAvailability.UNAVAILABLE
            snapshot = CIObservationSnapshot(subject, request, cycle, captured_epoch, availability, unavailable_reason=f"GitHub CI request failed ({availability.value})")
        except Exception as exc:
            snapshot = CIObservationSnapshot(subject, request, cycle, captured_epoch, ObservationAvailability.PARTIAL, unavailable_reason=str(exc))
        if captured_epoch != phase.epoch:
            snapshot = CIObservationSnapshot(subject, request, cycle, captured_epoch, ObservationAvailability.SUPERSEDED, unavailable_reason="completion was fenced by newer webhook evidence", diagnostic_facts=snapshot.facts or snapshot.diagnostic_facts)
        phase.cache[key] = snapshot
        logger.debug(f"CI observation cycle={cycle} epoch={phase.epoch} repo={repository} pr={pr_number} head={head_sha} availability={snapshot.availability.value} reason=network-fetch")
        return snapshot


def approve_waiting_deployment(api: Any, token: str, repository: str, run_id: int, run_attempt: int, head_sha: str) -> bool | None:
    """Freshly revalidate and approve one authoritative waiting execution."""
    owner, repo = repository.split("/", 1)
    credential = _credential_identity(token)
    lock_key = (repository, run_id)
    with _approval_guard:
        lock = _approval_locks.setdefault(lock_key, threading.Lock())
    with lock:
        try:
            run = api.actions.get_workflow_run(owner, repo, run_id)
            if not isinstance(run, dict) or run.get("id") != run_id or run.get("run_attempt") != run_attempt or run.get("head_sha") != head_sha or run.get("status") != "waiting":
                return False
            pending = api.actions.get_pending_deployments_for_run(owner, repo, run_id)
            if not isinstance(pending, list):
                return None
            try:
                environment_ids = tuple(sorted({int(item["environment"]["id"]) for item in pending}))
            except (KeyError, TypeError, ValueError):
                return None
            if not environment_ids:
                return False
            identity = (repository, run_id, run_attempt, environment_ids, credential)
            if identity in _confirmed_approvals:
                return True
            if identity in _indeterminate_approvals:
                return None
            try:
                api.actions.review_pending_deployments_for_run(owner, repo, run_id, environment_ids=list(environment_ids), state="approved", comment="Auto-approved by Auto-Coder")
            except Exception:
                _indeterminate_approvals.add(identity)
                return None
            _confirmed_approvals.add(identity)
            return True
        except Exception:
            return None
