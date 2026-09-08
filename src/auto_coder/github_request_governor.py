"""Process-local admission policy for controller-owned GitHub API traffic."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .logger_config import get_logger
from .util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestOutcome,
    GitHubRequestRefused,
    GitHubResponseMetadata,
    RequestProvenance,
    normalize_api_origin,
)

logger = get_logger(__name__)

REQUESTS_PER_MINUTE = 300
MUTATIONS_PER_MINUTE = 60
MUTATIONS_PER_HOUR = 400
MUTATION_SPACING_SECONDS = 1.0


class GitHubRequestDeferred(GitHubRequestRefused):
    """A typed, definitely-not-sent admission deferral."""

    def __init__(self, context: GitHubRequestContext, reason: str, retry_at: float) -> None:
        self.reason = reason
        self.retry_at = retry_at
        outcome = GitHubRequestOutcome(
            context,
            None,
            GitHubApiOutcome.REFUSED,
            RequestProvenance.NETWORK,
            DeliveryCertainty.DEFINITELY_NOT_SENT,
            GitHubResponseMetadata(),
            0.0,
            message=reason,
        )
        super().__init__(outcome)


@dataclass
class _OriginState:
    attempts: deque[float] = field(default_factory=deque)
    mutations: deque[float] = field(default_factory=deque)
    in_flight: str | None = None
    in_flight_kind: str | None = None
    last_mutation_completion: float | None = None
    cooldown_until: float = 0.0
    throttle_count: int = 0
    episode_active: bool = False
    post_cooldown_attempts: set[str] = field(default_factory=set)


class GitHubRequestGovernor:
    """One synchronized rolling-window governor shared by all credential roles."""

    def __init__(self, *, monotonic=time.monotonic, wall_time=time.time) -> None:
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._lock = threading.RLock()
        self._origins: dict[str, _OriginState] = {}

    def _state(self, origin: str) -> _OriginState:
        return self._origins.setdefault(normalize_api_origin(origin), _OriginState())

    @staticmethod
    def _trim(values: deque[float], now: float, window: float) -> None:
        while values and values[0] <= now - window:
            values.popleft()

    def admit(self, context: GitHubRequestContext) -> bool:
        """Charge one actual attempt or raise a pre-transmission deferral."""
        now = self._monotonic()
        origin = normalize_api_origin(context.api_origin)
        with self._lock:
            state = self._state(origin)
            self._trim(state.attempts, now, 60.0)
            self._trim(state.mutations, now, 3600.0)
            reason = ""
            eligible = now
            if state.cooldown_until > now:
                reason, eligible = "rate_limit_cooldown", state.cooldown_until
            elif state.in_flight is not None:
                # The exact end of an active wire attempt is unknowable.
                reason, eligible = "request_in_flight", now
            elif len(state.attempts) >= REQUESTS_PER_MINUTE:
                reason, eligible = "request_rolling_window", state.attempts[0] + 60.0
            elif context.kind == "mutation":
                recent_mutations = sum(value > now - 60.0 for value in state.mutations)
                if recent_mutations >= MUTATIONS_PER_MINUTE:
                    reason = "mutation_minute_window"
                    eligible = list(state.mutations)[-recent_mutations] + 60.0
                elif len(state.mutations) >= MUTATIONS_PER_HOUR:
                    reason, eligible = "mutation_hour_window", state.mutations[0] + 3600.0
                elif state.last_mutation_completion is not None and now < state.last_mutation_completion + MUTATION_SPACING_SECONDS:
                    reason, eligible = "mutation_spacing", state.last_mutation_completion + MUTATION_SPACING_SECONDS
            if reason:
                self._log(context, "deferred", reason, eligible, now)
                raise GitHubRequestDeferred(context, reason, self._wall_time() + max(0.0, eligible - now))

            state.attempts.append(now)
            if context.kind == "mutation":
                state.mutations.append(now)
            state.in_flight = context.attempt_id
            state.in_flight_kind = context.kind
            if state.episode_active and now >= state.cooldown_until:
                state.post_cooldown_attempts.add(context.attempt_id)
            self._log(context, "admitted", "eligible", now, now)
            return True

    def observe(self, outcome: GitHubRequestOutcome) -> None:
        """Atomically release concurrency and apply all fresh response evidence."""
        if outcome.provenance is not RequestProvenance.NETWORK or outcome.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT:
            return
        now = self._monotonic()
        origin = normalize_api_origin(outcome.context.api_origin)
        throttled = outcome.classification in {
            GitHubApiOutcome.PRIMARY_THROTTLED,
            GitHubApiOutcome.SECONDARY_THROTTLED,
            GitHubApiOutcome.THROTTLED,
        }
        with self._lock:
            state = self._state(origin)
            if state.in_flight == outcome.context.attempt_id:
                if state.in_flight_kind == "mutation":
                    state.last_mutation_completion = now
                state.in_flight = None
                state.in_flight_kind = None

            if throttled:
                state.episode_active = True
                state.throttle_count += 1
                local_deadline = now + min(3600.0, 60.0 * 2 ** (state.throttle_count - 1))
                deadlines = [state.cooldown_until, local_deadline]
                if outcome.metadata.retry_after_seconds is not None:
                    deadlines.append(now + outcome.metadata.retry_after_seconds)
                if outcome.metadata.rate_limit_remaining == 0 and outcome.metadata.rate_limit_reset is not None:
                    deadlines.append(now + max(0.0, outcome.metadata.rate_limit_reset - self._wall_time()))
                state.cooldown_until = max(deadlines)
                self._log(outcome.context, "cooldown", "throttle_observation", state.cooldown_until, now)
            elif outcome.classification is GitHubApiOutcome.SUCCESS and outcome.metadata.rate_limit_remaining == 0:
                reset_delay = None
                if outcome.metadata.rate_limit_reset is not None:
                    reset_delay = outcome.metadata.rate_limit_reset - self._wall_time()
                state.cooldown_until = max(state.cooldown_until, now + (reset_delay if reset_delay is not None and reset_delay > 0 else 60.0))
                self._log(outcome.context, "cooldown", "successful_remaining_zero", state.cooldown_until, now)
            elif outcome.classification is GitHubApiOutcome.SUCCESS and outcome.context.attempt_id in state.post_cooldown_attempts:
                state.episode_active = False
                state.throttle_count = 0
            state.post_cooldown_attempts.discard(outcome.context.attempt_id)

    def _log(self, context: GitHubRequestContext, decision: str, reason: str, eligible: float, now: float) -> None:
        retry_at = self._wall_time() + max(0.0, eligible - now)
        diagnostic = {
            "decision": decision,
            "origin": normalize_api_origin(context.api_origin),
            "attempt": context.attempt_id,
            "kind": context.kind,
            "delay_reason": reason,
            "next_eligible_at": datetime.fromtimestamp(retry_at, timezone.utc).isoformat(),
        }
        logger.bind(github_governor=diagnostic).info("github_governor_diagnostic {}", json.dumps(diagnostic, sort_keys=True))
