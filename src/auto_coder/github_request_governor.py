"""Durable admission policy for controller-owned GitHub API traffic."""

from __future__ import annotations

import fcntl
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

from .logger_config import get_logger
from .util.github_request_outcome import DeliveryCertainty, GitHubApiOutcome, GitHubRequestContext, GitHubRequestOutcome, GitHubRequestRefused, GitHubResponseMetadata, RequestProvenance, normalize_api_origin

logger = get_logger(__name__)
REQUESTS_PER_MINUTE = 300
MUTATIONS_PER_MINUTE = 60
MUTATIONS_PER_HOUR = 400
MUTATION_SPACING_SECONDS = 1.0
RECOVERY_COOLDOWN_SECONDS = 60.0
SCHEMA_VERSION = 2
ADMISSION_WAIT_BUDGET_SECONDS = 90.0
ADMISSION_POLL_CEILING_SECONDS = 0.5
ADMISSION_POLL_FLOOR_SECONDS = 0.01
# Deferrals this governor imposes on itself, every one of which resolves
# without any further GitHub cooperation: an attempt completing, one second of
# mutation spacing elapsing, or a rolling window edge passing. They describe
# when a request may be sent, never that it may not be sent at all.
SELF_RESOLVING_DEFERRALS = frozenset(
    {
        "request_in_flight",
        "mutation_spacing",
        "request_rolling_window",
        "mutation_minute_window",
        "mutation_hour_window",
    }
)


class GitHubRequestDeferred(GitHubRequestRefused):
    """A typed, definitely-not-sent admission deferral."""

    def __init__(self, context: GitHubRequestContext, reason: str, retry_at: float) -> None:
        self.reason = reason
        self.retry_at = retry_at
        super().__init__(GitHubRequestOutcome(context, None, GitHubApiOutcome.REFUSED, RequestProvenance.NETWORK, DeliveryCertainty.DEFINITELY_NOT_SENT, GitHubResponseMetadata(), 0.0, message=reason))


class GovernorStateError(RuntimeError):
    """The required durable governor state cannot be safely used."""


@dataclass(frozen=True)
class _OriginState:
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    throttle_count: int = 0
    episode_active: bool = False
    last_mutation_completion: float | None = None


def default_governor_path() -> Path:
    """Return the controller-wide governor path, independent of its cwd."""
    configured = os.environ.get("AUTO_CODER_RUNTIME_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".auto-coder" / "runtime"
    return root / "github" / "request_governor.sqlite3"


class GitHubRequestGovernor:
    """One synchronized, durable rolling-window governor for all credentials."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        store_path: Path | None = None,
        wait_budget: float = ADMISSION_WAIT_BUDGET_SECONDS,
        waiter: Callable[[float], None] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._wall_time = wall_time
        self.path = store_path or default_governor_path()
        self._wait_budget = wait_budget
        self._waiter = waiter
        self._admission_wake = threading.Condition()
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._incarnation_id = uuid.uuid4().hex
        self._lifetime_file: BinaryIO | None = None
        self._ownership_active = False
        self._unavailable_reason: str | None = None
        self._base_monotonic = self._checked_time(monotonic(), "monotonic clock")
        self._base_wall = self._checked_time(wall_time(), "UTC clock")
        try:
            self._open_and_recover()
        except Exception as exc:
            self._fail_closed(f"state initialization failed: {exc}")

    @staticmethod
    def _checked_time(value: float, name: str) -> float:
        if not math.isfinite(value):
            raise GovernorStateError(f"invalid {name}")
        return value

    @staticmethod
    def _stored_number(value: object, label: str) -> float:
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise GovernorStateError(f"invalid stored {label}")
        return float(value)

    @staticmethod
    def _valid_owner_id(value: object) -> bool:
        return isinstance(value, str) and len(value) == 32 and all(character in "0123456789abcdef" for character in value)

    def _now(self) -> float:
        elapsed = self._checked_time(self._monotonic(), "monotonic clock") - self._base_monotonic
        if elapsed < 0:
            raise GovernorStateError("monotonic clock moved backward")
        return self._base_wall + elapsed

    class _Transaction:
        def __init__(self, owner: "GitHubRequestGovernor") -> None:
            self.owner = owner

        def __enter__(self) -> None:
            assert self.owner._connection is not None
            self.owner._connection.execute("BEGIN IMMEDIATE")

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            assert self.owner._connection is not None
            self.owner._connection.execute("ROLLBACK" if exc_type else "COMMIT")

    def _transaction(self) -> _Transaction:
        return self._Transaction(self)

    def _open_and_recover(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lifetime_dir = self.path.parent / "owners"
        lifetime_dir.mkdir(mode=0o770, exist_ok=True)
        lifetime_path = lifetime_dir / f"{self._incarnation_id}.lock"
        lifetime = open(lifetime_path, "a+b")
        fcntl.flock(lifetime.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        self._lifetime_file = lifetime
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        self._connection = connection
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise GovernorStateError("SQLite integrity check failed")
        self._initialize_or_migrate()
        row = connection.execute("SELECT schema_version, last_logical_utc FROM governor_metadata WHERE singleton=1").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            raise GovernorStateError("incompatible governor schema")
        current_monotonic = self._checked_time(self._monotonic(), "monotonic clock")
        elapsed = current_monotonic - self._base_monotonic
        if elapsed < 0:
            raise GovernorStateError("monotonic clock moved backward")
        self._base_wall = max(self._base_wall + elapsed, self._stored_number(row[1], "UTC checkpoint"))
        self._base_monotonic = current_monotonic
        with self._transaction():
            self._checkpoint(self._now())
        self._ownership_active = True

    def _initialize_or_migrate(self) -> None:
        assert self._connection is not None
        connection = self._connection
        connection.execute("BEGIN EXCLUSIVE")
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                statements = (
                    "CREATE TABLE governor_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL, last_logical_utc REAL NOT NULL)",
                    """CREATE TABLE origin_state (
                    origin TEXT PRIMARY KEY, cooldown_until_utc REAL NOT NULL DEFAULT 0,
                    cooldown_reason TEXT NOT NULL DEFAULT '', throttle_count INTEGER NOT NULL DEFAULT 0,
                    episode_active INTEGER NOT NULL DEFAULT 0 CHECK(episode_active IN (0,1)),
                    last_mutation_completion_utc REAL
                )""",
                    """CREATE TABLE reservations (
                    origin TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('read','mutation')), admitted_utc REAL NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
                    recovered INTEGER NOT NULL DEFAULT 0 CHECK(recovered IN (0,1)),
                    post_cooldown INTEGER NOT NULL DEFAULT 0 CHECK(post_cooldown IN (0,1)),
                    owner_id TEXT,
                    PRIMARY KEY(origin, attempt_id)
                )""",
                    "CREATE INDEX reservations_budget ON reservations(origin, admitted_utc)",
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute("INSERT INTO governor_metadata VALUES (1, ?, ?)", (SCHEMA_VERSION, self._base_wall))
            else:
                if "governor_metadata" not in tables:
                    raise GovernorStateError("existing store has no supported schema")
                metadata = connection.execute("SELECT schema_version, last_logical_utc FROM governor_metadata WHERE singleton=1").fetchone()
                if metadata is None or metadata[0] not in (1, SCHEMA_VERSION):
                    raise GovernorStateError("incompatible governor schema")
                self._stored_number(metadata[1], "UTC checkpoint")
                if metadata[0] == 1:
                    self._validate_legacy_store(tables)
                    connection.execute("ALTER TABLE reservations ADD COLUMN owner_id TEXT")
                    now = max(self._now(), float(metadata[1]))
                    for origin, attempt_id in connection.execute("SELECT origin, attempt_id FROM reservations WHERE resolved=0 AND recovered=0").fetchall():
                        state = self._read_state(str(origin))
                        deadline = max(state.cooldown_until, now + RECOVERY_COOLDOWN_SECONDS)
                        connection.execute("UPDATE origin_state SET cooldown_until_utc=?, cooldown_reason='unresolved_attempt_recovery' WHERE origin=?", (deadline, origin))
                        connection.execute("UPDATE reservations SET recovered=1 WHERE origin=? AND attempt_id=?", (origin, attempt_id))
                    connection.execute("UPDATE governor_metadata SET schema_version=?, last_logical_utc=MAX(last_logical_utc, ?)", (SCHEMA_VERSION, now))
                self._validate_current_store(tables)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def _validate_legacy_store(self, tables: set[str]) -> None:
        required = {"governor_metadata", "origin_state", "reservations"}
        if not required.issubset(tables):
            raise GovernorStateError("legacy store is missing required tables")
        assert self._connection is not None
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(reservations)")}
        if not {"origin", "attempt_id", "kind", "admitted_utc", "resolved", "recovered", "post_cooldown"}.issubset(columns):
            raise GovernorStateError("legacy store is missing required columns")
        for origin, attempt, kind, admitted, resolved, recovered, post in self._connection.execute("SELECT origin, attempt_id, kind, admitted_utc, resolved, recovered, post_cooldown FROM reservations"):
            if not isinstance(origin, str) or normalize_api_origin(origin) != origin or not isinstance(attempt, str) or kind not in ("read", "mutation") or resolved not in (0, 1) or recovered not in (0, 1) or post not in (0, 1):
                raise GovernorStateError("invalid legacy reservation")
            self._stored_number(admitted, "reservation timestamp")
            if self._connection.execute("SELECT 1 FROM origin_state WHERE origin=?", (origin,)).fetchone() is None:
                raise GovernorStateError("legacy reservation has no origin state")
        for origin in (row[0] for row in self._connection.execute("SELECT origin FROM origin_state")):
            self._read_state(str(origin))

    def _validate_current_store(self, tables: set[str]) -> None:
        assert self._connection is not None
        if not {"governor_metadata", "origin_state", "reservations"}.issubset(tables):
            raise GovernorStateError("current store is missing required tables")
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(reservations)")}
        if "owner_id" not in columns:
            raise GovernorStateError("current store is missing ownership schema")
        metadata_count = self._connection.execute("SELECT COUNT(*) FROM governor_metadata").fetchone()
        if metadata_count != (1,):
            raise GovernorStateError("current store has invalid metadata")
        for origin, attempt, kind, admitted, resolved, recovered, post, owner in self._connection.execute("SELECT origin, attempt_id, kind, admitted_utc, resolved, recovered, post_cooldown, owner_id FROM reservations"):
            if (
                not isinstance(origin, str)
                or normalize_api_origin(origin) != origin
                or not isinstance(attempt, str)
                or kind not in ("read", "mutation")
                or resolved not in (0, 1)
                or recovered not in (0, 1)
                or post not in (0, 1)
                or (resolved == 0 and recovered == 0 and not self._valid_owner_id(owner))
            ):
                raise GovernorStateError("invalid current reservation")
            self._stored_number(admitted, "reservation timestamp")
            if self._connection.execute("SELECT 1 FROM origin_state WHERE origin=?", (origin,)).fetchone() is None:
                raise GovernorStateError("current reservation has no origin state")
        for (origin,) in self._connection.execute("SELECT origin FROM origin_state").fetchall():
            if not isinstance(origin, str) or normalize_api_origin(origin) != origin:
                raise GovernorStateError("invalid stored origin")
            self._read_state(origin)

    def _read_state(self, origin: str) -> _OriginState:
        assert self._connection is not None
        row = self._connection.execute("SELECT cooldown_until_utc, cooldown_reason, throttle_count, episode_active, last_mutation_completion_utc FROM origin_state WHERE origin=?", (origin,)).fetchone()
        if row is None:
            self._connection.execute("INSERT INTO origin_state(origin) VALUES (?)", (origin,))
            return _OriginState()
        cooldown = self._stored_number(row[0], "cooldown deadline")
        completion = None if row[4] is None else self._stored_number(row[4], "mutation completion")
        if not isinstance(row[1], str) or not isinstance(row[2], int) or row[2] < 0 or row[3] not in (0, 1):
            raise GovernorStateError("invalid stored origin state")
        return _OriginState(cooldown, row[1], row[2], bool(row[3]), completion)

    def _checkpoint(self, now: float) -> None:
        assert self._connection is not None
        self._connection.execute("UPDATE governor_metadata SET last_logical_utc=MAX(last_logical_utc, ?) WHERE singleton=1", (now,))

    def _cleanup(self, now: float) -> None:
        assert self._connection is not None
        self._connection.execute("DELETE FROM reservations WHERE admitted_utc <= ? AND (resolved=1 OR recovered=1)", (now - 3600.0,))
        self._connection.execute("DELETE FROM origin_state WHERE cooldown_until_utc <= ? AND COALESCE(last_mutation_completion_utc, 0) <= ? AND throttle_count=0 AND origin NOT IN (SELECT origin FROM reservations)", (now, now - MUTATION_SPACING_SECONDS))

    def _recover_terminated(self, now: float) -> None:
        """Recover only reservations whose incarnation lifetime lock is released."""
        assert self._connection is not None
        rows = self._connection.execute("SELECT DISTINCT owner_id FROM reservations WHERE resolved=0 AND recovered=0").fetchall()
        for (owner_id,) in rows:
            if not self._valid_owner_id(owner_id):
                raise GovernorStateError("unresolved reservation has indeterminate ownership")
            assert isinstance(owner_id, str)
            owner_path = self.path.parent / "owners" / f"{owner_id}.lock"
            try:
                owner_file = open(owner_path, "rb")
            except OSError as exc:
                raise GovernorStateError(f"ownership evidence unavailable for {owner_id}") from exc
            try:
                try:
                    fcntl.flock(owner_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    for origin, attempt in self._connection.execute(
                        "SELECT origin, attempt_id FROM reservations WHERE owner_id=? AND resolved=0 AND recovered=0",
                        (owner_id,),
                    ):
                        self._diagnostic(str(origin), str(attempt), "deferred", "live_owner_contention", now, now, incarnation=owner_id)
                    continue
                owned = self._connection.execute(
                    "SELECT origin, attempt_id FROM reservations WHERE owner_id=? AND resolved=0 AND recovered=0",
                    (owner_id,),
                ).fetchall()
                for origin, attempt in owned:
                    state = self._read_state(str(origin))
                    deadline = max(state.cooldown_until, now + RECOVERY_COOLDOWN_SECONDS)
                    self._connection.execute(
                        "UPDATE origin_state SET cooldown_until_utc=?, cooldown_reason='unresolved_attempt_recovery' WHERE origin=?",
                        (deadline, origin),
                    )
                    self._connection.execute(
                        "UPDATE reservations SET recovered=1 WHERE origin=? AND attempt_id=? AND owner_id=? AND resolved=0 AND recovered=0",
                        (origin, attempt, owner_id),
                    )
                    self._diagnostic(str(origin), str(attempt), "recovered", "confirmed_orphan_recovery", deadline, now, incarnation=owner_id)
            except OSError as exc:
                raise GovernorStateError(f"ownership verification failed for {owner_id}") from exc
            finally:
                owner_file.close()

    def close(self) -> None:
        """Release this controller incarnation after its process work has stopped."""
        with self._lock:
            release_lifetime = not self._ownership_active
            if self._connection is not None:
                try:
                    unresolved = self._connection.execute(
                        "SELECT 1 FROM reservations WHERE owner_id=? AND resolved=0 AND recovered=0 LIMIT 1",
                        (self._incarnation_id,),
                    ).fetchone()
                    release_lifetime = unresolved is None
                except sqlite3.Error:
                    release_lifetime = False
                self._connection.close()
                self._connection = None
            # Once this incarnation has admitted work, losing database access
            # cannot prove that its transports have stopped.  Keep lifetime
            # evidence until process termination rather than allowing another
            # participant to recover a possibly live request.
            if self._lifetime_file is not None and release_lifetime:
                self._lifetime_file.close()
                self._lifetime_file = None

    def admit(self, context: GitHubRequestContext, announce_deferral: bool = True) -> bool:
        """Durably reserve one actual attempt before transport may send it.

        This is a single atomic attempt at the reservation: it never waits.
        ``announce_deferral`` exists only so a caller that is already waiting
        out a self-resolving deferral does not re-log the same decision on
        every poll; the decision itself is unaffected.
        """
        origin = normalize_api_origin(context.api_origin)
        with self._lock:
            if self._unavailable_reason is not None:
                self._refuse_unavailable(context, origin)
            try:
                now = self._now()
                assert self._connection is not None
                with self._transaction():
                    self._recover_terminated(now)
                    self._cleanup(now)
                    state = self._read_state(origin)
                    rows = self._connection.execute("SELECT attempt_id, kind, admitted_utc, resolved, recovered FROM reservations WHERE origin=? ORDER BY admitted_utc", (origin,)).fetchall()
                    for row in rows:
                        self._stored_number(row[2], "reservation timestamp")
                    active = next((row for row in rows if row[3] == 0 and row[4] == 0), None)
                    attempts = [float(row[2]) for row in rows if float(row[2]) > now - 60.0]
                    minute = [float(row[2]) for row in rows if row[1] == "mutation" and float(row[2]) > now - 60.0]
                    hour = [float(row[2]) for row in rows if row[1] == "mutation" and float(row[2]) > now - 3600.0]
                    reason, eligible = "", now
                    if state.cooldown_until > now:
                        reason, eligible = "rate_limit_cooldown", state.cooldown_until
                    elif active is not None:
                        reason = "request_in_flight"
                    elif len(attempts) >= REQUESTS_PER_MINUTE:
                        reason, eligible = "request_rolling_window", min(attempts) + 60.0
                    elif context.kind == "mutation":
                        if len(minute) >= MUTATIONS_PER_MINUTE:
                            reason, eligible = "mutation_minute_window", min(minute) + 60.0
                        elif len(hour) >= MUTATIONS_PER_HOUR:
                            reason, eligible = "mutation_hour_window", min(hour) + 3600.0
                        elif state.last_mutation_completion is not None and now < state.last_mutation_completion + MUTATION_SPACING_SECONDS:
                            reason, eligible = "mutation_spacing", state.last_mutation_completion + MUTATION_SPACING_SECONDS
                    if not reason:
                        self._connection.execute("INSERT INTO reservations(origin, attempt_id, kind, admitted_utc, post_cooldown, owner_id) VALUES (?, ?, ?, ?, ?, ?)", (origin, context.attempt_id, context.kind, now, int(state.episode_active and now >= state.cooldown_until), self._incarnation_id))
                    self._checkpoint(now)
                if reason:
                    if announce_deferral:
                        self._diagnostic(origin, context.attempt_id, "deferred", reason, eligible, now)
                    raise GitHubRequestDeferred(context, reason, self._retry_at(eligible, now))
                self._diagnostic(origin, context.attempt_id, "admitted", "eligible", now, now)
                return True
            except GitHubRequestDeferred:
                raise
            except Exception as exc:
                self._fail_closed(f"reservation persistence failed: {exc}", origin)
                self._refuse_unavailable(context, origin)
        return False

    def observe(self, outcome: GitHubRequestOutcome) -> None:
        """Durably record completion and throttle evidence before another admission."""
        if outcome.provenance is not RequestProvenance.NETWORK or outcome.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT:
            return
        origin = normalize_api_origin(outcome.context.api_origin)
        with self._lock:
            if self._unavailable_reason is not None:
                return
            try:
                now = self._now()
                assert self._connection is not None
                throttled = outcome.classification in {GitHubApiOutcome.PRIMARY_THROTTLED, GitHubApiOutcome.SECONDARY_THROTTLED, GitHubApiOutcome.THROTTLED}
                with self._transaction():
                    state = self._read_state(origin)
                    reservation = self._connection.execute("SELECT kind, post_cooldown FROM reservations WHERE origin=? AND attempt_id=? AND owner_id=? AND resolved=0 AND recovered=0", (origin, outcome.context.attempt_id, self._incarnation_id)).fetchone()
                    if reservation is None:
                        return
                    self._connection.execute("UPDATE reservations SET resolved=1 WHERE origin=? AND attempt_id=? AND owner_id=?", (origin, outcome.context.attempt_id, self._incarnation_id))
                    completion = now if reservation[0] == "mutation" else state.last_mutation_completion
                    cooldown, cooldown_reason, count, episode = state.cooldown_until, state.cooldown_reason, state.throttle_count, state.episode_active
                    if throttled:
                        count, episode = count + 1, True
                        deadlines = [cooldown, now + min(3600.0, 60.0 * 2 ** (count - 1))]
                        if outcome.metadata.retry_after_seconds is not None:
                            deadlines.append(now + self._stored_number(outcome.metadata.retry_after_seconds, "Retry-After"))
                        if outcome.metadata.rate_limit_remaining == 0 and outcome.metadata.rate_limit_reset is not None:
                            deadlines.append(now + max(0.0, self._stored_number(outcome.metadata.rate_limit_reset, "rate limit reset") - self._wall_time()))
                        cooldown, cooldown_reason = max(deadlines), "throttle_observation"
                    elif outcome.classification is GitHubApiOutcome.SUCCESS and outcome.metadata.rate_limit_remaining == 0:
                        reset_delay = 0.0 if outcome.metadata.rate_limit_reset is None else max(0.0, self._stored_number(outcome.metadata.rate_limit_reset, "rate limit reset") - self._wall_time())
                        cooldown, cooldown_reason = max(cooldown, now + max(60.0, reset_delay)), "successful_remaining_zero"
                    elif outcome.classification is GitHubApiOutcome.SUCCESS and reservation[1]:
                        count, episode = 0, False
                    self._connection.execute("UPDATE origin_state SET cooldown_until_utc=?, cooldown_reason=?, throttle_count=?, episode_active=?, last_mutation_completion_utc=? WHERE origin=?", (cooldown, cooldown_reason, count, int(episode), completion, origin))
                    self._checkpoint(now)
                if cooldown > now:
                    self._diagnostic(origin, outcome.context.attempt_id, "cooldown", cooldown_reason, cooldown, now)
            except Exception as exc:
                self._fail_closed(f"outcome persistence failed: {exc}", origin)
            self._wake_waiters()

    def admit_blocking(self, context: GitHubRequestContext) -> bool:
        """Wait out this governor's own pacing instead of failing the caller.

        The controller runs several workers against one origin, so momentary
        self-imposed deferrals are the normal case rather than an error: an
        attempt is in flight, mutation spacing has not elapsed, a rolling
        window edge has not passed.  Raising those out of the transport turns
        ordinary pacing into spurious request failures for every concurrent
        read.  Waiting for the stated eligibility instead preserves the pacing
        policy exactly while keeping the caller's request truthful.

        Deferrals that are not self-resolving still propagate untouched:
        ``rate_limit_cooldown`` is real GitHub backpressure and
        ``governor_state_unavailable`` is unusable state, and their callers own
        durable resumption rather than an in-process wait.
        """
        deadline = self._wall_time() + self._wait_budget
        announce = True
        while True:
            try:
                return self.admit(context, announce_deferral=announce)
            except GitHubRequestDeferred as deferred:
                now = self._wall_time()
                remaining = deadline - now
                if deferred.reason not in SELF_RESOLVING_DEFERRALS or remaining <= 0:
                    if not announce:
                        self._exhausted(context, deferred)
                    raise
                announce = False
                self._wait_for_capacity(min(max(deferred.retry_at - now, ADMISSION_POLL_FLOOR_SECONDS), ADMISSION_POLL_CEILING_SECONDS, remaining))

    def _wait_for_capacity(self, seconds: float) -> None:
        if self._waiter is not None:
            self._waiter(seconds)
            return
        # A resolved reservation notifies immediately; the bounded timeout is
        # what covers another process resolving one in the shared store.
        with self._admission_wake:
            self._admission_wake.wait(seconds)

    def _wake_waiters(self) -> None:
        with self._admission_wake:
            self._admission_wake.notify_all()

    def _exhausted(self, context: GitHubRequestContext, deferred: GitHubRequestDeferred) -> None:
        diagnostic: dict[str, object] = {
            "decision": "wait_exhausted",
            "state_path": str(self.path),
            "origin": normalize_api_origin(context.api_origin),
            "attempt": context.attempt_id,
            "delay_reason": deferred.reason,
            "waited_seconds": self._wait_budget,
        }
        logger.bind(github_governor=diagnostic).warning("github_governor_diagnostic {}", json.dumps(diagnostic, sort_keys=True))

    def _retry_at(self, logical_deadline: float, logical_now: float) -> float:
        return self._wall_time() + max(0.0, logical_deadline - logical_now)

    def _refuse_unavailable(self, context: GitHubRequestContext, origin: str) -> None:
        now = self._now()
        self._diagnostic(origin, context.attempt_id, "refused", "governor_state_unavailable", now + 60.0, now, self._unavailable_reason)
        raise GitHubRequestDeferred(context, "governor_state_unavailable", self._wall_time() + 60.0)

    def _fail_closed(self, reason: str, origin: str = "unknown") -> None:
        self._unavailable_reason = reason
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None
        if self._lifetime_file is not None and not self._ownership_active:
            try:
                self._lifetime_file.close()
            except OSError:
                pass
            self._lifetime_file = None
        logger.bind(github_governor={"state_path": str(self.path), "origin": origin, "refusal_reason": reason}).error("GitHub governor state unavailable; network admission is closed")

    def _diagnostic(self, origin: str, attempt: str, decision: str, reason: str, eligible: float, now: float, refusal: str | None = None, incarnation: str | None = None) -> None:
        diagnostic: dict[str, object] = {
            "decision": decision,
            "state_path": str(self.path),
            "origin": origin,
            "attempt": attempt,
            "delay_reason": reason,
            "remaining_wait_seconds": max(0.0, eligible - now),
            "next_eligible_at": datetime.fromtimestamp(self._retry_at(eligible, now), timezone.utc).isoformat(),
        }
        if refusal:
            diagnostic["refusal_reason"] = refusal
        if incarnation:
            diagnostic["incarnation"] = incarnation
        logger.bind(github_governor=diagnostic).debug("github_governor_diagnostic {}", json.dumps(diagnostic, sort_keys=True))
