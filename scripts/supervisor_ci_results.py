"""Fail-closed pytest result inventory for Supervisor integration jobs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


class SupervisorResultInventory:
    """Record every collected case and its terminal pytest outcome."""

    def __init__(self, output: Path, allowed_skip_reasons: tuple[str, ...]) -> None:
        self.output = output
        self.allowed_skip_reasons = allowed_skip_reasons
        self.collected: list[str] = []
        self.outcomes: dict[str, str] = {}
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
        if report.failed:
            self.outcomes[report.nodeid] = "failed"
        elif report.skipped:
            reason = str(report.longrepr)
            if not any(allowed in reason for allowed in self.allowed_skip_reasons):
                self.outcomes[report.nodeid] = "forbidden-skip"
            else:
                self.outcomes[report.nodeid] = "skipped"
        elif report.when == "call":
            self.outcomes[report.nodeid] = report.outcome

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        missing = sorted(set(self.collected) - self.outcomes.keys())
        forbidden_skips = sorted(nodeid for nodeid, outcome in self.outcomes.items() if outcome == "forbidden-skip")
        payload = {
            "checkout_sha": os.environ.get("GITHUB_SHA", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "collected": self.collected,
            "outcomes": self.outcomes,
            "missing": missing,
            "first_case_phase_epoch": self.first_phase,
            "last_case_phase_epoch": self.last_phase,
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if not self.collected or missing or forbidden_skips:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    """Enable the inventory only for explicitly configured CI invocations."""
    output = os.environ.get("SUPERVISOR_RESULTS_PATH")
    if output:
        config.pluginmanager.register(
            SupervisorResultInventory(
                Path(output),
                tuple(json.loads(os.environ.get("SUPERVISOR_ALLOWED_SKIP_REASONS", "[]"))),
            ),
            "supervisor-result-inventory",
        )
