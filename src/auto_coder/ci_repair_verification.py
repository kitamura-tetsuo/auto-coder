"""Focused local verification for PR CI-failure repair.

CI owns full validation. After a corrective edit, the only local verification
this workflow performs is an explicit-file run (through the target repository's
configured test script) of each distinct failed test file reported by CI.
Nothing here ever invokes the runner without a file selector, and a target that
could not actually be executed is reported as unverified instead of passed.
"""

import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional

# Output fragments showing that a zero/non-zero exit did not execute the target.
_NO_EXECUTION_MARKERS = (
    "no tests ran",
    "collected 0 items",
    "no tests found",
    "no matching tests",
    "no test files found",
    "no tests to run",
)
# Output fragments showing that the configured runner rejected a per-file selector.
_UNSUPPORTED_SELECTOR_MARKERS = (
    "unrecognized arguments",
    "unrecognized option",
    "unknown option",
    "unknown argument",
)
# Shell exit codes meaning the command could not be launched at all.
_LAUNCH_FAILURE_CODES = (126, 127)
# Output fragments showing that the target container/runtime was unavailable (non-zero exit).
_LAUNCH_FAILURE_MARKERS = (
    "no such container",
    "is not running",
    "cannot connect to the docker daemon",
    "error response from daemon",
    "docker: command not found",
)


class TargetStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


@dataclass
class TargetResult:
    """Outcome of one explicitly selected failed-test file."""

    target: str
    status: TargetStatus
    reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FocusedVerification:
    """Results of one sweep over the known-failure set F on the current working state."""

    results: List[TargetResult] = field(default_factory=list)

    @property
    def failed(self) -> List[TargetResult]:
        return [r for r in self.results if r.status is TargetStatus.FAILED]

    @property
    def unverified(self) -> List[TargetResult]:
        return [r for r in self.results if r.status is TargetStatus.UNVERIFIED]

    @property
    def all_passed(self) -> bool:
        """True only when F is non-empty and every file in it actually ran and passed."""
        return bool(self.results) and all(r.status is TargetStatus.PASSED for r in self.results)

    def describe(self) -> List[str]:
        lines = []
        for r in self.results:
            suffix = f" ({r.reason})" if r.reason else ""
            lines.append(f"Focused check {r.status.value}: {r.target}{suffix}")
        return lines


def dedupe_targets(targets: Optional[Iterable[str]]) -> List[str]:
    """Deduplicate failed-test identities, preserving order and every distinct file."""
    seen: Dict[str, None] = {}
    for target in targets or []:
        normalized = str(target).strip()
        if normalized:
            seen.setdefault(normalized, None)
    return list(seen)


def classify_target_result(target: str, raw: Dict[str, Any]) -> TargetResult:
    """Classify a runner result for an explicit-file run without trusting a bare exit code."""
    command = str(raw.get("command") or "")
    return_code = raw.get("return_code", raw.get("returncode", -1))
    if not command or command == "none":
        return TargetResult(target, TargetStatus.UNVERIFIED, "local test execution was not performed", raw)
    if raw.get("test_file") != target:
        return TargetResult(target, TargetStatus.UNVERIFIED, "runner did not execute the selected target", raw)
    if isinstance(return_code, int) and (return_code < 0 or return_code in _LAUNCH_FAILURE_CODES):
        return TargetResult(target, TargetStatus.UNVERIFIED, f"test command could not be launched or timed out (exit {return_code})", raw)
    combined = f"{raw.get('output', '')}\n{raw.get('errors', '')}".lower()
    if not raw.get("success") and any(marker in combined for marker in _LAUNCH_FAILURE_MARKERS):
        return TargetResult(target, TargetStatus.UNVERIFIED, "test runtime or container could not be launched", raw)
    if any(marker in combined for marker in _NO_EXECUTION_MARKERS):
        return TargetResult(target, TargetStatus.UNVERIFIED, "runner reported no matching tests", raw)
    if not raw.get("success") and any(marker in combined for marker in _UNSUPPORTED_SELECTOR_MARKERS):
        return TargetResult(target, TargetStatus.UNVERIFIED, "configured test script does not support per-file selection", raw)
    if raw.get("success"):
        return TargetResult(target, TargetStatus.PASSED, "", raw)
    return TargetResult(target, TargetStatus.FAILED, "", raw)


def verify_targets(targets: Iterable[str], runner: Callable[..., Dict[str, Any]], config: Any) -> FocusedVerification:
    """Run every distinct target through the explicit-file runner; never unscoped."""
    verification = FocusedVerification()
    for target in dedupe_targets(targets):
        if not os.path.exists(target):
            verification.results.append(TargetResult(target, TargetStatus.UNVERIFIED, "target file not found in the working tree"))
            continue
        try:
            raw = runner(config, test_file=target)
        except Exception as exc:  # a launch problem is unavailability, not an application failure
            verification.results.append(TargetResult(target, TargetStatus.UNVERIFIED, f"local test execution raised: {exc}"))
            continue
        verification.results.append(classify_target_result(target, raw))
    return verification


def follow_up_budget_exhausted(max_attempts: Any, used: int) -> bool:
    """Whether the shared follow-up correction budget is spent (unbounded stays unbounded)."""
    try:
        limit = float(max_attempts)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(limit):
        return False
    return used >= int(limit)
