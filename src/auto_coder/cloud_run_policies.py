"""
Provider-specific `CloudRunPolicy` implementations.

Kept separate from `cloud_run.py` so that module stays provider-agnostic;
this module is where concrete providers plug into the generic lifecycle
abstraction (see issue #1606).
"""

from __future__ import annotations

from typing import Dict, Optional

from .cloud_run import CloudRunEvent, CloudRunPolicy

MANUAL_RETRY_REASON = "manual"
"""Reason marker for an explicit human-driven or otherwise separately
authorized attempt transition (see `CodexCloudRunPolicy`)."""

EMPTY_PR_REASON = "empty_pr"
"""Reason marker for an event proposing a new attempt because a PR
associated with a `CloudRun` was found to have zero effective diff."""


class CodexCloudRunPolicy(CloudRunPolicy):
    """Manual-only retry policy for Codex Cloud.

    Operational experience with Codex Cloud is still too limited to define
    reliable automatic unrecoverable-state detection, so every persisted
    Codex Cloud run is treated as recoverable from Auto-Coder's perspective:
    `RUNNING`, `PAUSED`, `FAILED`, `UNKNOWN`, a stalled/no-progress timeout,
    an empty pull request, or the absence of a PR must never by themselves
    authorize a new attempt. Only an event explicitly marked as a
    manual/authorized transition (`event.reason == MANUAL_RETRY_REASON`) is
    allowed through.
    """

    def allow_new_attempt(self, event: CloudRunEvent) -> bool:
        return event.reason == MANUAL_RETRY_REASON


_POLICIES_BY_PROVIDER: Dict[str, CloudRunPolicy] = {
    "codex-cloud": CodexCloudRunPolicy(),
}


def get_policy_for_provider(provider: str) -> Optional[CloudRunPolicy]:
    """Return the registered `CloudRunPolicy` for `provider`, if any.

    This is the single place that maps a provider name to its lifecycle
    policy so generic Issue/PR processing code never needs its own
    provider-name checks (see issue #1607, REQ-008).
    """
    return _POLICIES_BY_PROVIDER.get(provider)
