"""Runtime scheduling layer for terminal PR-backed implementation slot reclamation.

Issue #2146 defines the retirement predicate/transaction and Issue #2147
defines the read-only evidence collector. Neither decides *when* to run: as
of this module's introduction (Issue #2148), nothing in production called
either. This module supplies that missing scheduling layer:

* real production trigger sites record a durable, level-triggered
  reevaluation obligation for an owner (``schedule_reevaluation``);
* a due-obligation consumer periodically services pending obligations by
  collecting a fresh observation and handing it to the existing retirement
  transaction (``run_due_reclamation_checks``);
* daemon startup recovers persisted obligations and seeds fresh ones for
  every active Issue-owned reservation, so a PR that closed while the
  daemon was offline is not silently skipped (``recover_obligations_at_startup``).

This module never reimplements terminality logic: it only calls
``collect_retirement_observation`` and ``retire_implementation_slot`` from
the prerequisite modules. It never closes/reopens/merges Issues or PRs,
changes labels, cancels remote work, or starts a replacement implementation.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .implementation_retirement import RetirementStatus, retire_implementation_slot
from .implementation_retirement_observer import collect_retirement_observation
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .logger_config import get_logger

logger = get_logger(__name__)

# REQ-003: while the daemon runs, a still-pending owner is re-checked no
# later than 60 seconds after its previous check finished.
RECLAMATION_RECHECK_SECONDS = 60.0

_SHARED_FILE_MODE = 0o660


@dataclass(frozen=True)
class ReclamationObligation:
    """One durable reevaluation obligation for a terminal-PR-backed owner."""

    repository: str
    owner_kind: str
    owner_number: int
    incarnation: str
    next_due_at: float
    last_reason: Optional[str] = None

    @property
    def owner(self) -> ImplementationOwner:
        return ImplementationOwner(self.owner_kind, self.owner_number)

    @property
    def key(self) -> str:
        return f"{self.owner_kind}:{self.owner_number}"


class ReclamationObligationStore:
    """Persist reclamation obligations in a JSON file sibling to the slot store.

    Follows ``ImplementationSlotRepository``'s own ``_read``/``_write``
    persistence style (a single JSON object, atomic ``os.replace``, a
    process-local lock guarding the read-modify-write) rather than inventing
    a new locking scheme. Cross-process exclusivity for the mutation this
    store participates in (a due-check's retire decision) is provided by
    ``ImplementationSlotRepository.serialize`` around the owner, exactly as
    ordinary admission/mutation paths already use it.
    """

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._lock = threading.RLock()

    @classmethod
    def for_slots(cls, slots: ImplementationSlotRepository) -> "ReclamationObligationStore":
        path = slots.storage_path.parent / f"{slots.storage_path.stem}_reclamation.json"
        return cls(path)

    def _read(self) -> Dict[str, Dict[str, object]]:
        try:
            os.stat(self.storage_path)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.error(f"Cannot stat reclamation obligation state at '{self.storage_path}': {exc}")
            return {}
        try:
            with io.open(self.storage_path, "r", encoding="utf-8") as state_file:
                value = json.load(state_file)
            if not isinstance(value, dict):
                raise ValueError("reclamation obligation state root must be an object")
            return value
        except (json.JSONDecodeError, UnicodeError, ValueError, OSError) as exc:
            logger.error(f"Cannot parse reclamation obligation state at '{self.storage_path}': {exc}")
            return {}

    def _write(self, records: Dict[str, Dict[str, object]]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _SHARED_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as state_file:
            json.dump(records, state_file, indent=2, sort_keys=True)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, self.storage_path)

    def upsert(self, obligation: ReclamationObligation) -> None:
        """Insert or refresh an obligation for its owner key.

        Coalesces duplicate wakes for the same incarnation: when an
        obligation for the same incarnation is already pending, the earlier
        of the two ``next_due_at`` values wins, so a fresh wake never delays
        an already-earlier-due check and a reschedule never overrides an
        earlier still-pending due time. A different (newer) incarnation
        always supersedes whatever was recorded, since the store is keyed by
        owner, not by incarnation.
        """
        with self._lock:
            records = self._read()
            existing = records.get(obligation.key)
            effective = obligation
            if existing is not None and existing.get("incarnation") == obligation.incarnation:
                existing_due = existing.get("next_due_at")
                if isinstance(existing_due, (int, float)) and existing_due < obligation.next_due_at:
                    effective = ReclamationObligation(
                        repository=obligation.repository,
                        owner_kind=obligation.owner_kind,
                        owner_number=obligation.owner_number,
                        incarnation=obligation.incarnation,
                        next_due_at=float(existing_due),
                        last_reason=obligation.last_reason,
                    )
            records[obligation.key] = {
                "repository": effective.repository,
                "owner_kind": effective.owner_kind,
                "owner_number": effective.owner_number,
                "incarnation": effective.incarnation,
                "next_due_at": effective.next_due_at,
                "last_reason": effective.last_reason,
            }
            self._write(records)

    def replace(self, obligation: ReclamationObligation) -> None:
        """Unconditionally write *obligation*, without earliest-due coalescing.

        Used by the due-check consumer to reschedule an obligation it just
        finished servicing: that obligation's own previous (now-past)
        ``next_due_at`` must never win a "keep the earlier one" comparison
        against the freshly computed next check time. Coalescing toward the
        earliest requested time is only meaningful for concurrent external
        wake requests (``schedule_reevaluation``), not for the due-check's
        own post-processing reschedule.
        """
        with self._lock:
            records = self._read()
            records[obligation.key] = {
                "repository": obligation.repository,
                "owner_kind": obligation.owner_kind,
                "owner_number": obligation.owner_number,
                "incarnation": obligation.incarnation,
                "next_due_at": obligation.next_due_at,
                "last_reason": obligation.last_reason,
            }
            self._write(records)

    def clear(self, owner: ImplementationOwner, incarnation: str) -> None:
        """Clear the obligation for *owner* only if it still matches *incarnation*.

        An older incarnation's completion must never consume a newer
        incarnation's obligation (REQ-003, AS-005).
        """
        with self._lock:
            records = self._read()
            existing = records.get(owner.key)
            if existing is None or existing.get("incarnation") != incarnation:
                return
            records.pop(owner.key, None)
            self._write(records)

    def all(self) -> tuple[ReclamationObligation, ...]:
        with self._lock:
            records = self._read()
        obligations = []
        for record in records.values():
            try:
                owner_number_raw: Any = record["owner_number"]
                next_due_at_raw: Any = record["next_due_at"]
                if isinstance(owner_number_raw, bool) or not isinstance(owner_number_raw, (int, float, str)):
                    raise ValueError("owner_number must be numeric")
                if isinstance(next_due_at_raw, bool) or not isinstance(next_due_at_raw, (int, float, str)):
                    raise ValueError("next_due_at must be numeric")
                last_reason_raw = record.get("last_reason")
                obligations.append(
                    ReclamationObligation(
                        repository=str(record["repository"]),
                        owner_kind=str(record["owner_kind"]),
                        owner_number=int(owner_number_raw),
                        incarnation=str(record["incarnation"]),
                        next_due_at=float(next_due_at_raw),
                        last_reason=last_reason_raw if isinstance(last_reason_raw, str) else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(f"Skipping malformed reclamation obligation record: {record!r}")
                continue
        return tuple(obligations)

    def due(self, now: Optional[float] = None) -> tuple[ReclamationObligation, ...]:
        now = time.time() if now is None else now
        return tuple(obligation for obligation in self.all() if obligation.next_due_at <= now)


def schedule_reevaluation(
    owner: ImplementationOwner,
    slots: ImplementationSlotRepository,
    store: Optional[ReclamationObligationStore] = None,
    *,
    reason: Optional[str] = None,
    due_at: Optional[float] = None,
) -> bool:
    """Make *owner* eligible for a reconciliation check (REQ-001, REQ-003).

    Call this from every trigger site: authoritative PR close/merge
    observations, Auto-Coder's own empty/stale-PR closure completion,
    execution/provider-work completion, and terminal early-return/explicit
    single-target processing paths. This never starts a new execution and
    never blocks on capacity, so it remains callable while capacity is full
    or over limit.

    Returns False (a no-op) when *owner* is not currently an active
    Issue-owned reservation -- there is nothing to schedule.
    """
    if owner.kind != "issue":
        return False
    store = store or ReclamationObligationStore.for_slots(slots)
    incarnation = slots.owner_incarnation(owner)
    if incarnation is None:
        return False
    now = time.time()
    obligation = ReclamationObligation(
        repository=slots.repo_name,
        owner_kind=owner.kind,
        owner_number=owner.number,
        incarnation=incarnation,
        next_due_at=due_at if due_at is not None else now,
        last_reason=reason,
    )
    store.upsert(obligation)
    logger.debug(f"Scheduled reclamation reevaluation for {owner.key} (incarnation={incarnation}, reason={reason})")
    return True


def _rescheduled(obligation: ReclamationObligation, now: float, reason: str) -> ReclamationObligation:
    return ReclamationObligation(
        repository=obligation.repository,
        owner_kind=obligation.owner_kind,
        owner_number=obligation.owner_number,
        incarnation=obligation.incarnation,
        next_due_at=now + RECLAMATION_RECHECK_SECONDS,
        last_reason=reason,
    )


def run_due_reclamation_checks(
    slots: ImplementationSlotRepository,
    store: Optional[ReclamationObligationStore] = None,
    *,
    github_client: Any,
    jules_client: Optional[Any] = None,
    cloud_manager: Optional[Any] = None,
    cloud_run_store: Optional[Any] = None,
    on_capacity_freed: Optional[Callable[[], None]] = None,
    now: Optional[float] = None,
) -> int:
    """Service every currently-due reclamation obligation once (REQ-003, REQ-004).

    Each owner's check runs under ``slots.serialize(owner)``, the same
    per-owner cross-process lock ordinary admission/mutation paths already
    use, so two overlapping checks for the same incarnation never run.

    Returns the number of obligations this call actually released.
    """
    store = store or ReclamationObligationStore.for_slots(slots)
    now = time.time() if now is None else now
    released = 0
    for obligation in store.due(now):
        owner = obligation.owner
        with slots.serialize(owner):
            current_incarnation = slots.owner_incarnation(owner)
            if current_incarnation != obligation.incarnation:
                # Either already retired, or retired-and-recreated under a
                # new incarnation. Only clear this stale entry when it is
                # still the one on file for this incarnation (a fresher
                # obligation for the new incarnation, if any, is untouched).
                store.clear(owner, obligation.incarnation)
                continue
            try:
                observation = collect_retirement_observation(
                    owner,
                    slots,
                    github_client,
                    jules_client,
                    cloud_manager,
                    cloud_run_store,
                )
            except Exception as exc:
                logger.error(f"Reclamation evidence collection failed for {owner.key} " f"(incarnation={obligation.incarnation}): {exc}; keeping the pending obligation")
                store.replace(_rescheduled(obligation, now, reason=f"observation-error:{type(exc).__name__}"))
                continue

            if observation is None:
                # Not (or no longer) an in-scope Issue-owned PR-backed
                # reservation. Nothing further to reevaluate here.
                store.clear(owner, obligation.incarnation)
                continue

            result = retire_implementation_slot(slots, observation)
            if result.status is RetirementStatus.RELEASED:
                logger.info(f"Reclaimed terminal implementation slot {owner.key} " f"(incarnation={obligation.incarnation}, reason={result.diagnostic})")
                store.clear(owner, obligation.incarnation)
                released += 1
                if on_capacity_freed is not None:
                    try:
                        on_capacity_freed()
                    except Exception as exc:
                        logger.error(f"on_capacity_freed callback failed after reclaiming {owner.key}: {exc}")
            else:
                # RETAINED_ACTIVE / RETAINED_UNKNOWN / STALE_OBSERVATION: keep
                # the obligation alive for another pass. Expected outcomes
                # (still active or unknown) are informational, not errors --
                # a repeated unchanged pending check must not spam ERROR logs.
                logger.info(f"Reclamation still pending for {owner.key}: {result.status.value} - {result.diagnostic}")
                store.replace(_rescheduled(obligation, now, reason=result.status.value))
    return released


def recover_obligations_at_startup(
    slots: ImplementationSlotRepository,
    store: Optional[ReclamationObligationStore] = None,
    *,
    now: Optional[float] = None,
) -> int:
    """At daemon startup, make every active Issue-owned slot immediately due (REQ-002).

    Persisted obligations from a previous run are already in ``store`` and
    naturally become due once their ``next_due_at`` has passed; this also
    seeds a due obligation for every currently active Issue-owned
    reservation regardless of whether one was already pending, so an owner
    whose PR closed while the daemon was offline -- and is therefore absent
    from the open-PR enumeration -- is not silently skipped.

    This only *schedules* a check; ``run_due_reclamation_checks`` performs
    the actual fresh observation and re-validates against the live store
    before doing anything, so this never imports retired history as active
    and never forces a release for an owner with live/uncertain work.
    """
    store = store or ReclamationObligationStore.for_slots(slots)
    now = time.time() if now is None else now
    seeded = 0
    for owner in slots.active_owners():
        if owner.kind != "issue":
            continue
        incarnation = slots.owner_incarnation(owner)
        if incarnation is None:
            continue
        store.upsert(
            ReclamationObligation(
                repository=slots.repo_name,
                owner_kind=owner.kind,
                owner_number=owner.number,
                incarnation=incarnation,
                next_due_at=now,
                last_reason="startup-recovery",
            )
        )
        seeded += 1
    return seeded


def schedule_reevaluation_for_pr_owner(
    pr_data: Dict[str, Any],
    slots: ImplementationSlotRepository,
    github_client: Any,
    store: Optional[ReclamationObligationStore] = None,
    *,
    reason: Optional[str] = None,
) -> bool:
    """Resolve *pr_data*'s owner and schedule its reevaluation, if applicable.

    Convenience wrapper for PR-closure trigger sites (webhook delivery,
    Auto-Coder's own empty/stale-PR closure, explicit single-target
    processing) that only have the closed/merged PR's data on hand, not
    already-resolved owner identity. Resolving a PR to a non-Issue owner (a
    standalone PR, an unrecognized/foreign PR, or resolution failure) is a
    no-op: only ordinary Issue-owned reservations are in scope for this
    reclamation path (REQ-009).
    """
    try:
        owner = slots.resolve_owner("pr", pr_data, github_client)
    except Exception as exc:
        logger.debug(f"Cannot resolve PR owner for reclamation scheduling: {exc}")
        return False
    if owner.kind != "issue":
        return False
    return schedule_reevaluation(owner, slots, store, reason=reason)
