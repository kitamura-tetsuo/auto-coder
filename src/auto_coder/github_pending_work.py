"""Durable application obligations interrupted by GitHub availability.

The request governor controls wire admission.  This store controls the distinct
application question: whether unfinished semantic work may be forgotten.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from .logger_config import get_logger
from .util.github_request_outcome import GitHubApiOutcome, GitHubRequestError

logger = get_logger(__name__)
_LOCK = threading.Lock()
_DEFAULT_STORE: PendingWorkStore | None = None
_DEFAULT_SCHEDULER: PendingWorkScheduler | None = None
MAX_THROTTLED_RETRIES = 3
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class ObligationStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"


class PendingReason(str, Enum):
    THROTTLED = "throttled"
    ADMISSION_DEFERRED = "admission_deferred"
    AUTHENTICATION = "authentication_failure"
    FORBIDDEN = "forbidden"
    INDETERMINATE = "indeterminate_delivery"
    RETRIES_EXHAUSTED = "throttle_retries_exhausted"


@dataclass(frozen=True)
class WorkIdentity:
    repository: str
    entity: str
    stage: str
    revision: str = ""

    def key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PendingObligation:
    identity: WorkIdentity
    reason: PendingReason
    not_before: float
    unfinished_effects: tuple[str, ...] = field(default_factory=tuple)
    throttle_attempts: int = 0
    last_error: str = ""
    status: str = ObligationStatus.WAITING.value

    @property
    def automatically_retryable(self) -> bool:
        return self.reason in {PendingReason.THROTTLED, PendingReason.ADMISSION_DEFERRED}


def default_pending_work_path() -> Path:
    return Path.home() / ".auto-coder" / "github_pending_work.db"


class PendingWorkPersistenceError(RuntimeError):
    """Persistence uncertainty; dependent effects must stop (fail closed)."""


class PendingWorkStore:
    """SQLite-backed semantic retry queue, shared across workers and restarts."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_pending_work_path()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS github_pending_work (
            work_key TEXT PRIMARY KEY, repository TEXT NOT NULL, entity TEXT NOT NULL,
            stage TEXT NOT NULL, revision TEXT NOT NULL, reason TEXT NOT NULL,
            not_before REAL NOT NULL, unfinished_effects TEXT NOT NULL,
            throttle_attempts INTEGER NOT NULL, last_error TEXT NOT NULL,
            updated_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting')"""
        )
        # Preflight-schema databases predate the status column. A pre-existing
        # row cannot be safely interpreted as anything but eligible for
        # resumption, so it defaults to waiting rather than being dropped.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(github_pending_work)")}
        if "status" not in columns:
            connection.execute("ALTER TABLE github_pending_work ADD COLUMN status TEXT NOT NULL DEFAULT 'waiting'")
        return connection

    def defer(
        self,
        identity: WorkIdentity,
        error: GitHubRequestError,
        unfinished_effects: tuple[str, ...],
        *,
        governor_deadline: float | None = None,
        now: float | None = None,
    ) -> PendingObligation:
        """Atomically retain work; only actual throttle responses spend retries."""
        current_time = time.time() if now is None else now
        classification = error.outcome.classification
        if classification is GitHubApiOutcome.REFUSED:
            reason = PendingReason.ADMISSION_DEFERRED
        elif classification is GitHubApiOutcome.AUTHENTICATION_FAILURE:
            reason = PendingReason.AUTHENTICATION
        elif classification is GitHubApiOutcome.FORBIDDEN:
            reason = PendingReason.FORBIDDEN
        elif error.outcome.delivery.value == "indeterminate_after_possible_send":
            reason = PendingReason.INDETERMINATE
        else:
            reason = PendingReason.THROTTLED
        retry_after = error.outcome.metadata.retry_after_seconds or 0.0
        due = max(current_time + retry_after, governor_deadline or current_time)
        try:
            with _LOCK, self._connect() as connection:
                row = connection.execute(
                    "SELECT throttle_attempts, not_before, unfinished_effects FROM github_pending_work WHERE work_key=?",
                    (identity.key(),),
                ).fetchone()
                attempts = int(row[0]) if row else 0
                if reason is PendingReason.THROTTLED:
                    attempts += 1
                    if attempts > MAX_THROTTLED_RETRIES:
                        reason = PendingReason.RETRIES_EXHAUSTED
                prior_due = float(row[1]) if row else 0.0
                prior_effects = tuple(json.loads(row[2])) if row else ()
                effects = tuple(dict.fromkeys((*prior_effects, *unfinished_effects)))
                # Authentication, forbidden, ambiguity and exhausted retries are
                # operational blocks: they are never timer-driven.
                if reason not in {PendingReason.THROTTLED, PendingReason.ADMISSION_DEFERRED}:
                    due = 0.0
                scheduled_for = max(due, prior_due) if reason in {PendingReason.THROTTLED, PendingReason.ADMISSION_DEFERRED} else 0.0
                # A defer() call always follows work that is no longer actively
                # dispatched (either newly retained or re-armed after a stage
                # handler reported an error), so it releases any running claim.
                obligation = PendingObligation(identity, reason, scheduled_for, effects, attempts, str(error), ObligationStatus.WAITING.value)
                connection.execute(
                    "INSERT OR REPLACE INTO github_pending_work VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity.key(),
                        identity.repository,
                        identity.entity,
                        identity.stage,
                        identity.revision,
                        reason.value,
                        obligation.not_before,
                        json.dumps(effects),
                        attempts,
                        obligation.last_error,
                        current_time,
                        obligation.status,
                    ),
                )
                return obligation
        except Exception as exc:
            logger.error("Could not persist GitHub pending obligation {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be persisted") from exc

    def due(self, now: float | None = None) -> list[PendingObligation]:
        """Obligations eligible for a fresh dispatch: waiting and past their deadline."""
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                rows = connection.execute(
                    "SELECT repository,entity,stage,revision,reason,not_before,unfinished_effects,throttle_attempts,last_error " "FROM github_pending_work WHERE status=? AND not_before > 0 AND not_before <= ? ORDER BY not_before",
                    (ObligationStatus.WAITING.value, current_time),
                ).fetchall()
            return [PendingObligation(WorkIdentity(*row[:4]), PendingReason(row[4]), row[5], tuple(json.loads(row[6])), row[7], row[8]) for row in rows]
        except Exception as exc:
            logger.error("Could not read GitHub pending obligations: {}", exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be read") from exc

    def next_due_at(self) -> float | None:
        """Earliest not-before among waiting obligations, or None if none are scheduled."""
        try:
            with _LOCK, self._connect() as connection:
                row = connection.execute(
                    "SELECT MIN(not_before) FROM github_pending_work WHERE status=? AND not_before > 0",
                    (ObligationStatus.WAITING.value,),
                ).fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception as exc:
            logger.error("Could not read next GitHub pending obligation deadline: {}", exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be read") from exc

    def interrupted(self) -> list[PendingObligation]:
        """Obligations left 'running' by a process that stopped before completing dispatch."""
        try:
            with _LOCK, self._connect() as connection:
                rows = connection.execute(
                    "SELECT repository,entity,stage,revision,reason,not_before,unfinished_effects,throttle_attempts,last_error " "FROM github_pending_work WHERE status=?",
                    (ObligationStatus.RUNNING.value,),
                ).fetchall()
            return [PendingObligation(WorkIdentity(*row[:4]), PendingReason(row[4]), row[5], tuple(json.loads(row[6])), row[7], row[8], ObligationStatus.RUNNING.value) for row in rows]
        except Exception as exc:
            logger.error("Could not read interrupted GitHub pending obligations: {}", exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be read") from exc

    def all_pending(self) -> list[PendingObligation]:
        """Every retained obligation, for status/observability views."""
        try:
            with _LOCK, self._connect() as connection:
                rows = connection.execute("SELECT repository,entity,stage,revision,reason,not_before,unfinished_effects,throttle_attempts,last_error,status " "FROM github_pending_work ORDER BY updated_at").fetchall()
            return [PendingObligation(WorkIdentity(*row[:4]), PendingReason(row[4]), row[5], tuple(json.loads(row[6])), row[7], row[8], row[9]) for row in rows]
        except Exception as exc:
            logger.error("Could not read GitHub pending obligations: {}", exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be read") from exc

    def get(self, identity: WorkIdentity) -> PendingObligation | None:
        """Current state of one obligation, regardless of status or deadline."""
        try:
            with _LOCK, self._connect() as connection:
                row = connection.execute(
                    "SELECT reason,not_before,unfinished_effects,throttle_attempts,last_error,status FROM github_pending_work WHERE work_key=?",
                    (identity.key(),),
                ).fetchone()
            if row is None:
                return None
            return PendingObligation(identity, PendingReason(row[0]), row[1], tuple(json.loads(row[2])), row[3], row[4], row[5])
        except Exception as exc:
            logger.error("Could not read GitHub pending obligation {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be read") from exc

    def mark_running(self, identity: WorkIdentity) -> None:
        """Claim an obligation for an in-progress handler dispatch."""
        try:
            with _LOCK, self._connect() as connection:
                connection.execute(
                    "UPDATE github_pending_work SET status=?, updated_at=? WHERE work_key=?",
                    (ObligationStatus.RUNNING.value, time.time(), identity.key()),
                )
        except Exception as exc:
            logger.error("Could not mark GitHub pending obligation running {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be updated") from exc

    def mark_waiting(self, identity: WorkIdentity) -> None:
        """Release a running claim without changing its schedule or effects."""
        try:
            with _LOCK, self._connect() as connection:
                connection.execute(
                    "UPDATE github_pending_work SET status=?, updated_at=? WHERE work_key=?",
                    (ObligationStatus.WAITING.value, time.time(), identity.key()),
                )
        except Exception as exc:
            logger.error("Could not mark GitHub pending obligation waiting {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be updated") from exc

    def manual_retry(self, identity: WorkIdentity, now: float | None = None) -> PendingObligation | None:
        """Explicit operator retry: clear only the selected retry block/count.

        Confirmed effect receipts (``unfinished_effects`` already trimmed by
        ``complete_effect``) and the original reason are preserved; only the
        deadline and throttle counter are reset so the scheduler may offer the
        obligation to its stage handler again, which still goes through the
        shared governor for admission.
        """
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                row = connection.execute(
                    "SELECT reason,unfinished_effects,last_error FROM github_pending_work WHERE work_key=?",
                    (identity.key(),),
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    "UPDATE github_pending_work SET not_before=?, throttle_attempts=0, status=?, updated_at=? WHERE work_key=?",
                    (current_time, ObligationStatus.WAITING.value, current_time, identity.key()),
                )
                return PendingObligation(identity, PendingReason(row[0]), current_time, tuple(json.loads(row[1])), 0, row[2], ObligationStatus.WAITING.value)
        except Exception as exc:
            logger.error("Could not manually retry GitHub pending obligation {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be updated") from exc

    def complete_effect(self, identity: WorkIdentity, effect: str) -> bool:
        """Confirm one durable effect without completing its independent siblings."""
        try:
            with _LOCK, self._connect() as connection:
                row = connection.execute("SELECT unfinished_effects FROM github_pending_work WHERE work_key=?", (identity.key(),)).fetchone()
                if row is None:
                    return False
                remaining = tuple(item for item in json.loads(row[0]) if item != effect)
                if remaining:
                    connection.execute(
                        "UPDATE github_pending_work SET unfinished_effects=?, updated_at=? WHERE work_key=?",
                        (json.dumps(remaining), time.time(), identity.key()),
                    )
                else:
                    connection.execute("DELETE FROM github_pending_work WHERE work_key=?", (identity.key(),))
                return True
        except Exception as exc:
            logger.error("Could not complete GitHub pending effect {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending effect could not be persisted") from exc

    def supersede(self, identity: WorkIdentity) -> None:
        """Discard effects after an authoritative refresh proves the revision stale."""
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("DELETE FROM github_pending_work WHERE work_key=?", (identity.key(),))
        except Exception as exc:
            logger.error("Could not supersede GitHub pending obligation {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be updated") from exc


def get_pending_work_store() -> PendingWorkStore:
    """Return the process-wide durable obligation store."""
    global _DEFAULT_STORE
    with _LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = PendingWorkStore()
        return _DEFAULT_STORE


@dataclass(frozen=True)
class StageOutcome:
    """What a stage handler accomplished for one dispatch or recovery call.

    ``error`` re-defers the obligation's still-unfinished effects through the
    normal semantic-retry accounting; a handler must not resend a mutation
    itself when delivery is merely uncertain, so an indeterminate result
    should be reported through ``error`` (``DeliveryCertainty.INDETERMINATE``)
    rather than silently retried inside the handler.
    """

    completed_effects: tuple[str, ...] = ()
    error: GitHubRequestError | None = None
    superseded: bool = False
    governor_deadline: float | None = None


class StageHandler(Protocol):
    """A registered semantic stage that owns current-input validation and delivery."""

    def dispatch(self, obligation: PendingObligation) -> StageOutcome:
        """Attempt the obligation's unfinished effects for the first time this claim."""
        ...

    def recover(self, obligation: PendingObligation) -> StageOutcome:
        """Resume an obligation left 'running' by a controller that stopped mid-dispatch.

        Delivery may or may not have happened; this must not blindly resend a
        mutation and should consult the owning stage's own delivery evidence.
        """
        ...


class PendingWorkScheduler:
    """Daemon service that dispatches durable obligations to their stage handlers.

    The governor owns HTTP admission; this service owns *when* a registered
    semantic stage may resume retained work, waking on due deadlines or an
    explicit :meth:`wake` call (for example from an admission-change
    notification) rather than polling tightly.
    """

    def __init__(
        self,
        store: PendingWorkStore,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._poll_interval = poll_interval
        self._clock = clock
        self._handlers: dict[str, StageHandler] = {}
        self._wake_event: asyncio.Event | None = None
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def register_handler(self, stage: str, handler: StageHandler) -> None:
        """Register the owning handler for a semantic stage before the loop starts."""
        self._handlers[stage] = handler

    def wake(self) -> None:
        """Interrupt an idle wait, e.g. after an admission-change notification."""
        event = self._wake_event
        if event is not None and not event.is_set():
            event.set()

    def manual_retry(self, identity: WorkIdentity) -> PendingObligation | None:
        """Operator-triggered retry of one selected retry-blocked obligation."""
        obligation = self._store.manual_retry(identity)
        if obligation is not None:
            self.wake()
        return obligation

    def snapshot(self) -> list[dict[str, object]]:
        """Observable view of every retained obligation for daemon status reporting."""
        try:
            obligations = self._store.all_pending()
        except PendingWorkPersistenceError as exc:
            logger.error("Pending-work status snapshot unavailable: {}", exc)
            return []
        result: list[dict[str, object]] = []
        for obligation in obligations:
            blocked_reason: str | None = None
            if obligation.status == ObligationStatus.WAITING.value and obligation.identity.stage not in self._handlers:
                blocked_reason = "no registered stage handler"
            elif not obligation.automatically_retryable and obligation.status == ObligationStatus.WAITING.value:
                blocked_reason = f"operational block: {obligation.reason.value}"
            result.append(
                {
                    "repository": obligation.identity.repository,
                    "entity": obligation.identity.entity,
                    "stage": obligation.identity.stage,
                    "revision": obligation.identity.revision,
                    "reason": obligation.reason.value,
                    "status": obligation.status,
                    "not_before": obligation.not_before,
                    "unfinished_effects": list(obligation.unfinished_effects),
                    "throttle_attempts": obligation.throttle_attempts,
                    "blocked": blocked_reason,
                }
            )
        return result

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Recover interrupted work, then dispatch due obligations until shutdown.

        Cancellation (e.g. from the daemon's drain) leaves any in-flight
        dispatch's row as 'running' in the store rather than marking it
        waiting or complete, so a genuine crash and a cooperative shutdown are
        recovered identically on the next start.
        """
        self._wake_event = asyncio.Event()
        try:
            await self._recover_interrupted()
            while not shutdown_event.is_set():
                delay = self._next_delay()
                wake_wait = asyncio.ensure_future(self._wake_event.wait())
                shutdown_wait = asyncio.ensure_future(shutdown_event.wait())
                try:
                    await asyncio.wait({wake_wait, shutdown_wait}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (wake_wait, shutdown_wait):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(wake_wait, shutdown_wait, return_exceptions=True)
                self._wake_event.clear()
                if shutdown_event.is_set():
                    return
                await self._dispatch_due()
        finally:
            # Cancelling here (rather than letting them run loose) keeps a
            # forced shutdown and a real crash equivalent: an in-flight claim
            # is interrupted before it can mark completion, so it is picked up
            # by _recover_interrupted() on the next start instead of being
            # silently abandoned as an orphaned task.
            pending_tasks = list(self._tasks)
            for pending_task in pending_tasks:
                pending_task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

    def _next_delay(self) -> float:
        try:
            upcoming = self._store.next_due_at()
        except PendingWorkPersistenceError:
            return self._poll_interval
        now = self._clock()
        if upcoming is None or upcoming <= now:
            # A due-now obligation that cannot make progress (missing handler,
            # or a handler that just re-deferred it) must not spin the loop at
            # zero delay; wait at least one poll interval unless an explicit
            # wake (e.g. admission became available) cuts the wait short.
            return self._poll_interval
        return min(upcoming - now, max(self._poll_interval, 3600.0))

    async def _recover_interrupted(self) -> None:
        try:
            interrupted = await asyncio.to_thread(self._store.interrupted)
        except PendingWorkPersistenceError as exc:
            logger.error("Could not read interrupted GitHub pending work for recovery: {}", exc)
            return
        for obligation in interrupted:
            handler = self._handlers.get(obligation.identity.stage)
            if handler is None:
                logger.warning("No stage handler registered for interrupted obligation {}; leaving blocked", obligation.identity.key())
                continue
            await self._claim_and_run(obligation, handler.recover)

    async def _dispatch_due(self) -> None:
        try:
            due = await asyncio.to_thread(self._store.due, self._clock())
        except PendingWorkPersistenceError as exc:
            logger.error("Could not read due GitHub pending work: {}", exc)
            return
        for obligation in due:
            key = obligation.identity.key()
            with self._in_flight_lock:
                if key in self._in_flight:
                    continue
                self._in_flight.add(key)
            handler = self._handlers.get(obligation.identity.stage)
            if handler is None:
                logger.warning("No stage handler registered for stage {!r}; obligation {} stays blocked", obligation.identity.stage, key)
                with self._in_flight_lock:
                    self._in_flight.discard(key)
                continue
            self._spawn(obligation, handler.dispatch)

    def _spawn(self, obligation: PendingObligation, entry: Callable[[PendingObligation], StageOutcome]) -> None:
        task = asyncio.ensure_future(self._run_claimed(obligation, entry))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _claim_and_run(self, obligation: PendingObligation, entry: Callable[[PendingObligation], StageOutcome]) -> None:
        key = obligation.identity.key()
        with self._in_flight_lock:
            if key in self._in_flight:
                return
            self._in_flight.add(key)
        await self._run_claimed(obligation, entry)

    async def _run_claimed(self, obligation: PendingObligation, entry: Callable[[PendingObligation], StageOutcome]) -> None:
        identity = obligation.identity
        key = identity.key()
        try:
            try:
                await asyncio.to_thread(self._store.mark_running, identity)
            except PendingWorkPersistenceError as exc:
                logger.error("Could not claim GitHub pending obligation {} for dispatch: {}", key, exc)
                return
            try:
                outcome = await asyncio.to_thread(entry, obligation)
            except Exception as exc:
                logger.opt(exception=True).error("Stage handler {!r} raised while processing {}: {}", identity.stage, key, exc)
                try:
                    await asyncio.to_thread(self._store.mark_waiting, identity)
                except PendingWorkPersistenceError:
                    pass
                return
            try:
                await asyncio.to_thread(self._apply_outcome, identity, outcome)
            except PendingWorkPersistenceError as exc:
                logger.error("Could not persist outcome for GitHub pending obligation {}: {}", key, exc)
                return
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(key)
        self.wake()

    def _apply_outcome(self, identity: WorkIdentity, outcome: StageOutcome) -> None:
        if outcome.superseded:
            self._store.supersede(identity)
            return
        for effect in outcome.completed_effects:
            self._store.complete_effect(identity, effect)
        if outcome.error is not None:
            current = self._store.get(identity)
            remaining = current.unfinished_effects if current is not None else ()
            self._store.defer(identity, outcome.error, remaining, governor_deadline=outcome.governor_deadline)
        elif self._store.get(identity) is not None:
            self._store.mark_waiting(identity)


def get_pending_work_scheduler() -> PendingWorkScheduler:
    """Return the process-wide pending-work scheduling service."""
    global _DEFAULT_SCHEDULER
    with _LOCK:
        if _DEFAULT_SCHEDULER is None:
            _DEFAULT_SCHEDULER = PendingWorkScheduler(get_pending_work_store())
        return _DEFAULT_SCHEDULER
