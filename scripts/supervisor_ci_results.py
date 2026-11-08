"""Fail-closed pytest result inventory for Supervisor integration jobs."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from supervisor_ci_profiles import MANAGEMENT_SKIPS, OPERATOR_REQUIRED, anchors, check_plan

from auto_coder.ci_shard_plan import VerifiedPlan, read_plan


def _checkout_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


class SupervisorResultInventory:
    """Record every collected case and its terminal pytest outcome."""

    def __init__(self, output: Path, allowed_skips: dict[str, str], required_nodeids: tuple[str, ...], plan: VerifiedPlan | None = None, shard: int | None = None) -> None:
        self.output = output
        self.allowed_skips = allowed_skips
        self.required_nodeids = required_nodeids
        self.plan = plan
        self.shard = shard
        self.collected: list[str] = []
        self.outcomes: dict[str, str] = {}
        self.skip_reasons: dict[str, str] = {}
        self.terminal_counts: dict[str, int] = {}
        self.first_phase: float | None = None
        self.last_phase: float | None = None

    @pytest.hookimpl
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = [item.nodeid for item in session.items]

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        now = time.time()
        self.first_phase = now if self.first_phase is None else min(self.first_phase, now)
        self.last_phase = now if self.last_phase is None else max(self.last_phase, now)
        terminal = report.failed or report.skipped or report.when == "call"
        if terminal:
            self.terminal_counts[report.nodeid] = self.terminal_counts.get(report.nodeid, 0) + 1
        if report.failed:
            self.outcomes[report.nodeid] = "failed"
        elif report.skipped:
            reason = str(report.longrepr)
            self.skip_reasons[report.nodeid] = reason
            allowed = next(
                (allowed_reason for nodeid, allowed_reason in self.allowed_skips.items() if report.nodeid == nodeid or report.nodeid.startswith(f"{nodeid}[")),
                None,
            )
            if allowed is None or allowed not in reason:
                self.outcomes[report.nodeid] = "forbidden-skip"
            else:
                self.outcomes[report.nodeid] = "skipped"
        elif report.when == "call":
            self.outcomes[report.nodeid] = report.outcome

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        # The shard adapter selects from the complete inventory after regular
        # collection-finish hooks. session.items is the final actual selection.
        self.collected = [item.nodeid for item in session.items]
        missing_selection = sorted(set(self.required_nodeids) - set(self.collected))
        missing = sorted(set(self.collected) - self.outcomes.keys())
        forbidden_skips = sorted(nodeid for nodeid, outcome in self.outcomes.items() if outcome == "forbidden-skip")
        unexpected = sorted(set(self.outcomes) - set(self.collected))
        repeated = sorted(nodeid for nodeid, count in self.terminal_counts.items() if count != 1)
        expected = self.plan.shards[self.shard - 1] if self.plan is not None and self.shard is not None else None
        wrong_shard = expected is not None and tuple(self.collected) != expected
        actual_sha = _checkout_sha()
        wrong_source = self.plan is not None and actual_sha != self.plan.context.checkout_sha
        payload = {
            "schema_version": "supervisor-native-inventory-v2",
            "checkout_sha": actual_sha,
            "event_sha": os.environ.get("GITHUB_SHA", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "job_key": os.environ.get("GITHUB_JOB", ""),
            "runner_name": os.environ.get("RUNNER_NAME", ""),
            "profile": self.plan.context.profile_id if self.plan is not None else os.environ.get("SUPERVISOR_PROFILE", "worker"),
            "plan_digest": self.plan.digest if self.plan is not None else None,
            "plan_origin": self.plan.context.origin if self.plan is not None else None,
            "shard": self.shard,
            "collected": self.collected,
            "outcomes": self.outcomes,
            "skip_reasons": self.skip_reasons,
            "missing_selection": missing_selection,
            "missing": missing,
            "unexpected": unexpected,
            "repeated": repeated,
            "first_case_phase_epoch": self.first_phase,
            "last_case_phase_epoch": self.last_phase,
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if not self.collected or missing_selection or missing or forbidden_skips or unexpected or repeated or wrong_shard or wrong_source or len(set(self.collected)) != len(self.collected):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    """Enable the inventory only for explicitly configured CI invocations."""
    if os.environ.get("SUPERVISOR_CI_RESULT_MODE") == "collect":
        # Collection is not execution; a planner cannot mint a PASS receipt.
        return
    output = os.environ.get("SUPERVISOR_RESULTS_PATH")
    if output:
        plan = None
        shard = None
        plan_path = os.environ.get("SUPERVISOR_PLAN_PATH")
        shard_value = os.environ.get("SUPERVISOR_SHARD_ID")
        profile = os.environ.get("SUPERVISOR_PROFILE")
        if profile == "operator-upgrade" and plan_path is None and shard_value is None:
            if tuple(json.loads(os.environ.get("SUPERVISOR_REQUIRED_NODEIDS", "[]"))) != OPERATOR_REQUIRED:
                raise pytest.UsageError("Operator upgrade mandatory node registry differs from production profile")
            if json.loads(os.environ.get("SUPERVISOR_ALLOWED_SKIPS", "{}")) != {}:
                raise pytest.UsageError("Operator upgrade cannot authorize skipped tests")
        elif any((plan_path, shard_value, profile)) and not all((plan_path, shard_value, profile)):
            raise pytest.UsageError("Supervisor plan/profile/shard binding is incomplete")
        if plan_path is not None and shard_value is not None and profile is not None:
            try:
                plan, _raw = read_plan(Path(plan_path))
                shard = int(shard_value)
                check_plan(profile, Path(plan_path))
                if not 1 <= shard <= len(plan.shards):
                    raise ValueError("Supervisor shard ID is outside the plan")
                if os.environ.get("SUPERVISOR_REQUIRED_NODEIDS") != json.dumps(anchors(profile), separators=(",", ":")):
                    raise ValueError("full required anchor registry differs from profile")
                if json.loads(os.environ.get("SUPERVISOR_ALLOWED_SKIPS", "{}")) != (MANAGEMENT_SKIPS if profile == "management-self-update" else {}):
                    raise ValueError("approved duplicate management skips differ from profile")
            except (ValueError, OSError) as error:
                raise pytest.UsageError(f"Supervisor plan refused: {error}") from error
        required_full = tuple(json.loads(os.environ.get("SUPERVISOR_REQUIRED_NODEIDS", "[]")))
        required = tuple(node for node in required_full if node in plan.shards[shard - 1]) if plan is not None and shard is not None else required_full
        config.pluginmanager.register(
            SupervisorResultInventory(
                Path(output),
                json.loads(os.environ.get("SUPERVISOR_ALLOWED_SKIPS", "{}")),
                required,
                plan,
                shard,
            ),
            "supervisor-result-inventory",
        )
