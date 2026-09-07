"""Crash-safe initial-PR recovery for accepted Codex Cloud runs."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .cloud_run import CloudRun, CloudRunRepository
from .codex_observation import CodexObservationService, CodexRunObservation, PullRequestPresence
from .codex_wham_client import CodexWhamClient, FollowUpDeliveryOutcome
from .logger_config import get_logger

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 60.0
COMPLETION_GRACE_SECONDS = 120.0
REMINDER_PURPOSE = "initial-pr-publication:v1"


class RecoveryOutcome(str, Enum):
    OBSERVING = "observing"
    COMPLETION_GRACE = "completion_grace"
    REMINDER_RESERVED = "reminder_reserved"
    REMINDER_ACCEPTED = "reminder_accepted"
    REMINDER_REJECTED = "reminder_rejected"
    DELIVERY_INDETERMINATE = "delivery_indeterminate"
    PR_OBSERVED = "pr_observed"
    ATTENTION_NEEDED = "attention_needed"


@dataclass(frozen=True)
class RecoveryRecord:
    repository: str
    task_id: str
    state: RecoveryOutcome
    completion_turn: str = ""
    completion_user_turn: str = ""
    completion_observed_at: Optional[float] = None
    reminder_turn: str = ""
    reminder_user_turn: str = ""
    reminder_sent_at: Optional[float] = None
    pr_number: Optional[int] = None
    handoff_complete: bool = False
    reason: str = ""


class CodexPRRecoveryStore:
    """SQLite state and the one-way reservation guarding the WHAM POST."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path.home() / ".auto-coder" / "codex-pr-recovery.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS codex_pr_recovery (
                    repository TEXT NOT NULL, provider TEXT NOT NULL,
                    task_id TEXT NOT NULL, purpose TEXT NOT NULL,
                    state TEXT NOT NULL, completion_turn TEXT NOT NULL DEFAULT '',
                    completion_user_turn TEXT NOT NULL DEFAULT '',
                    completion_observed_at REAL, reminder_turn TEXT NOT NULL DEFAULT '',
                    reminder_user_turn TEXT NOT NULL DEFAULT '', reminder_sent_at REAL,
                    pr_number INTEGER, handoff_complete INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                    PRIMARY KEY(repository, provider, task_id, purpose))"""
            )

    def get(self, repository: str, task_id: str) -> Optional[RecoveryRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state,completion_turn,completion_user_turn,completion_observed_at," "reminder_turn,reminder_user_turn,reminder_sent_at,pr_number,handoff_complete,reason " "FROM codex_pr_recovery WHERE repository=? AND provider='codex-cloud' AND task_id=? AND purpose=?",
                (repository, task_id, REMINDER_PURPOSE),
            ).fetchone()
        if row is None:
            return None
        return RecoveryRecord(
            repository=repository,
            task_id=task_id,
            state=RecoveryOutcome(row[0]),
            completion_turn=str(row[1]),
            completion_user_turn=str(row[2]),
            completion_observed_at=row[3],
            reminder_turn=str(row[4]),
            reminder_user_turn=str(row[5]),
            reminder_sent_at=row[6],
            pr_number=row[7],
            handoff_complete=bool(row[8]),
            reason=str(row[9]),
        )

    def save_observation(
        self,
        run: CloudRun,
        state: RecoveryOutcome,
        *,
        completion_turn: str = "",
        completion_user_turn: str = "",
        completion_observed_at: Optional[float] = None,
        pr_number: Optional[int] = None,
        reason: str = "",
    ) -> bool:
        """Persist observation state without ever erasing a send reservation/publication."""
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                old = self.get(run.repo_name, run.task_id)
                if old and old.state in {RecoveryOutcome.REMINDER_RESERVED, RecoveryOutcome.REMINDER_ACCEPTED, RecoveryOutcome.REMINDER_REJECTED, RecoveryOutcome.DELIVERY_INDETERMINATE, RecoveryOutcome.PR_OBSERVED, RecoveryOutcome.ATTENTION_NEEDED}:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    """INSERT INTO codex_pr_recovery(repository,provider,task_id,purpose,state,
                       completion_turn,completion_user_turn,completion_observed_at,pr_number,reason,updated_at)
                       VALUES(?,'codex-cloud',?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(repository,provider,task_id,purpose) DO UPDATE SET
                       state=excluded.state,completion_turn=excluded.completion_turn,
                       completion_user_turn=excluded.completion_user_turn,
                       completion_observed_at=excluded.completion_observed_at,
                       pr_number=COALESCE(excluded.pr_number,codex_pr_recovery.pr_number),
                       reason=excluded.reason,updated_at=excluded.updated_at""",
                    (run.repo_name, run.task_id, REMINDER_PURPOSE, state.value, completion_turn, completion_user_turn, completion_observed_at, pr_number, reason, time.time()),
                )
                connection.execute("COMMIT")
                return True
        except Exception as exc:
            logger.error(f"Codex PR recovery store write failed for {run.repo_name}/#{run.issue_number}/{run.task_id}: {type(exc).__name__}")
            return False

    def reserve_send(self, run: CloudRun, turn: str, user_turn: str, now: float) -> bool:
        """Atomically spend the sole automatic POST budget."""
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state FROM codex_pr_recovery WHERE repository=? AND provider='codex-cloud' AND task_id=? AND purpose=?",
                    (run.repo_name, run.task_id, REMINDER_PURPOSE),
                ).fetchone()
                if row is None or row[0] != RecoveryOutcome.COMPLETION_GRACE.value:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "UPDATE codex_pr_recovery SET state=?,reminder_turn=?,reminder_user_turn=?,reminder_sent_at=?,updated_at=? " "WHERE repository=? AND provider='codex-cloud' AND task_id=? AND purpose=?",
                    (RecoveryOutcome.REMINDER_RESERVED.value, turn, user_turn, now, now, run.repo_name, run.task_id, REMINDER_PURPOSE),
                )
                connection.execute("COMMIT")
                return True
        except Exception as exc:
            logger.error(f"Codex PR recovery reservation failed for {run.repo_name}/#{run.issue_number}/{run.task_id}: {type(exc).__name__}")
            return False

    def transition(self, run: CloudRun, state: RecoveryOutcome, *, pr_number: Optional[int] = None, reason: str = "") -> bool:
        try:
            with self._connect() as connection:
                result = connection.execute(
                    "UPDATE codex_pr_recovery SET state=?,pr_number=COALESCE(?,pr_number),reason=?,updated_at=? " "WHERE repository=? AND provider='codex-cloud' AND task_id=? AND purpose=?",
                    (state.value, pr_number, reason, time.time(), run.repo_name, run.task_id, REMINDER_PURPOSE),
                )
                return result.rowcount == 1
        except Exception:
            return False

    def mark_handoff(self, run: CloudRun) -> bool:
        try:
            with self._connect() as connection:
                result = connection.execute(
                    "UPDATE codex_pr_recovery SET handoff_complete=1,updated_at=? WHERE repository=? AND provider='codex-cloud' AND task_id=? AND purpose=? AND state=?",
                    (time.time(), run.repo_name, run.task_id, REMINDER_PURPOSE, RecoveryOutcome.PR_OBSERVED.value),
                )
                return result.rowcount == 1
        except Exception:
            return False


class CodexPRRecoveryMonitor:
    """Poll only accepted, coherently bound runs and recover their initial PR."""

    def __init__(
        self,
        runs: CloudRunRepository,
        observations: CodexObservationService,
        wham: CodexWhamClient,
        store: CodexPRRecoveryStore,
        enqueue_pr: Callable[[int], Awaitable[bool]],
        *,
        now: Callable[[], float] = time.time,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        grace_period: float = COMPLETION_GRACE_SECONDS,
    ) -> None:
        self.runs, self.observations, self.wham, self.store = runs, observations, wham, store
        self.enqueue_pr, self.now, self.poll_interval, self.grace_period = enqueue_pr, now, poll_interval, grace_period
        self._next_due: dict[str, float] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _coherent(run: CloudRun, all_runs: list[CloudRun]) -> bool:
        latest = max((item.attempt for item in all_runs if item.issue_number == run.issue_number), default=run.attempt)
        return bool(run.provider == "codex-cloud" and run.submission_outcome == "accepted" and run.task_id and run.backend_name and run.environment_id and run.base_branch and run.attempt == latest)

    async def run(self, shutdown: asyncio.Event) -> None:
        """Recover immediately, then discover newly registered runs without webhooks."""
        while not shutdown.is_set():
            await self.tick(shutdown)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=min(1.0, self.poll_interval))
            except asyncio.TimeoutError:
                pass

    async def tick(self, shutdown: asyncio.Event) -> None:
        try:
            runs = await asyncio.to_thread(self.runs.list_all)
        except Exception as exc:
            logger.error(f"Codex PR recovery cannot read tracked runs: {type(exc).__name__}")
            return
        now = self.now()
        due = [run for run in runs if self._coherent(run, runs) and now >= self._next_due.get(run.task_id, 0)]
        await asyncio.gather(*(self._poll_locked(run, shutdown) for run in due), return_exceptions=True)

    async def _poll_locked(self, run: CloudRun, shutdown: asyncio.Event) -> None:
        lock = self._task_locks.setdefault(run.task_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                await self._poll(run, shutdown)
            except Exception as exc:
                logger.error(f"Codex PR recovery observation failed for {run.repo_name}/#{run.issue_number}/{run.task_id}: {type(exc).__name__}")
            finally:
                self._next_due[run.task_id] = self.now() + self.poll_interval

    async def _poll(self, run: CloudRun, shutdown: asyncio.Event) -> None:
        observation = await asyncio.to_thread(self.observations.observe, run)
        record = self.store.get(run.repo_name, run.task_id)
        pr = observation.pull_request
        if pr.presence in {PullRequestPresence.PR_PRESENT, PullRequestPresence.PREVIOUSLY_PUBLISHED} and pr.number:
            if record is None or record.state is not RecoveryOutcome.PR_OBSERVED:
                if not self.store.save_observation(run, RecoveryOutcome.PR_OBSERVED, pr_number=pr.number):
                    self.store.transition(run, RecoveryOutcome.PR_OBSERVED, pr_number=pr.number)
            updated = self.store.get(run.repo_name, run.task_id)
            if pr.presence is PullRequestPresence.PR_PRESENT and updated and not updated.handoff_complete and not shutdown.is_set():
                if await self.enqueue_pr(pr.number):
                    self.store.mark_handoff(run)
            return
        if observation.errors or observation.issue_state != "open" or pr.presence is not PullRequestPresence.NO_MATCHING_PR:
            logger.info(f"Codex PR recovery waiting for {run.repo_name}/#{run.issue_number}/{run.task_id}: state={observation.execution.state.value}, pr={pr.presence.value}, reminder={record.state.value if record else 'none'}")
            return
        if record and record.state in {RecoveryOutcome.REMINDER_REJECTED, RecoveryOutcome.ATTENTION_NEEDED, RecoveryOutcome.PR_OBSERVED}:
            return
        if record and record.state in {RecoveryOutcome.REMINDER_RESERVED, RecoveryOutcome.REMINDER_ACCEPTED, RecoveryOutcome.DELIVERY_INDETERMINATE}:
            self._observe_after_reminder(run, observation, record)
            return
        evidence = observation.execution
        if not evidence.recovery_eligible:
            if record and record.state is RecoveryOutcome.COMPLETION_GRACE:
                self.store.save_observation(run, RecoveryOutcome.OBSERVING, reason="new or non-completed activity observed")
            return
        now = self.now()
        if not record or record.completion_turn != evidence.assistant_turn_id:
            self.store.save_observation(run, RecoveryOutcome.COMPLETION_GRACE, completion_turn=evidence.assistant_turn_id, completion_user_turn=evidence.user_turn_id, completion_observed_at=now)
            return
        if record.completion_observed_at is None or now - record.completion_observed_at < self.grace_period or shutdown.is_set():
            return
        # Fresh pre-send evidence fences Issue closure, PR races, and new activity.
        fresh = await asyncio.to_thread(self.observations.observe, run)
        if (
            fresh.errors
            or fresh.issue_state != "open"
            or fresh.pull_request.presence is not PullRequestPresence.NO_MATCHING_PR
            or not fresh.execution.recovery_eligible
            or fresh.execution.assistant_turn_id != record.completion_turn
            or fresh.execution.user_turn_id != record.completion_user_turn
            or shutdown.is_set()
        ):
            return
        # Definite local failures remain retryable because no reservation or
        # outbound request has occurred yet.
        if not await asyncio.to_thread(self.wham.follow_up_preflight):
            return
        if not self.store.reserve_send(run, fresh.execution.assistant_turn_id, fresh.execution.user_turn_id, now):
            return
        result = await asyncio.to_thread(self.wham.send_follow_up, run.task_id, fresh.execution.assistant_turn_id, "Create PR", False)
        state = {FollowUpDeliveryOutcome.DELIVERED: RecoveryOutcome.REMINDER_ACCEPTED, FollowUpDeliveryOutcome.NOT_DELIVERED: RecoveryOutcome.REMINDER_REJECTED, FollowUpDeliveryOutcome.INDETERMINATE: RecoveryOutcome.DELIVERY_INDETERMINATE}[result.outcome]
        self.store.transition(run, state, reason=f"HTTP {result.status_code}" if result.status_code else result.outcome.value)

    def _observe_after_reminder(self, run: CloudRun, observation: CodexRunObservation, record: RecoveryRecord) -> None:
        if record.state is RecoveryOutcome.REMINDER_REJECTED:
            self.store.transition(run, RecoveryOutcome.ATTENTION_NEEDED, reason="Create PR reminder was rejected")
            return
        evidence = observation.execution
        # Causality requires a newly observed user turn as well as a new assistant
        # completion; mere unrelated assistant advancement cannot prove delivery.
        causally_new = bool(evidence.user_turn_id and evidence.user_turn_id != record.reminder_user_turn)
        if causally_new and evidence.recovery_eligible and evidence.assistant_turn_id != record.reminder_turn:
            prefix = f"continuation_completed:{evidence.assistant_turn_id}:"
            if not record.reason.startswith(prefix):
                self.store.transition(run, record.state, reason=f"{prefix}{self.now()}")
                return
            try:
                anchor = float(record.reason[len(prefix) :])
            except ValueError:
                return
            if self.now() - anchor >= self.grace_period:
                self.store.transition(run, RecoveryOutcome.ATTENTION_NEEDED, reason="Create PR continuation completed without a matching PR")
