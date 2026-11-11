"""Closed, non-secret startup failure facts shared by worker and its observers."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class StartupReconciliationFailure:
    """A sticky terminal startup disposition, bound by the enclosing worker identity."""

    failure_id: str
    reason: str = "startup_reconciliation_abandoned"


def observed_startup_failure(observation: object) -> StartupReconciliationFailure | None:
    """Recognize only a closed typed fact, never an error message or timeout."""
    if not isinstance(observation, dict):
        return None
    lifecycle, invocations = observation.get("lifecycle"), observation.get("invocations")
    if not isinstance(lifecycle, dict) or not isinstance(invocations, dict) or lifecycle.get("state") not in {"failed_awaiting_drain", "draining"} or lifecycle.get("startup_reconciled") is not False or lifecycle.get("daemon_serving") is not False or invocations.get("state") != "draining":
        return None
    return startup_failure_fact(lifecycle.get("startup_failure"))


def startup_failure_fact(failure: object) -> StartupReconciliationFailure | None:
    """Validate the closed durable fact independently of live observation state."""
    if not isinstance(failure, dict) or set(failure) != {"failure_id", "reason"} or failure.get("reason") != "startup_reconciliation_abandoned":
        return None
    identity = failure.get("failure_id")
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        return None
    return StartupReconciliationFailure(identity)
