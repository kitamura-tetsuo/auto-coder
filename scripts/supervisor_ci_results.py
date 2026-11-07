"""Fail-closed pytest result inventory for Supervisor integration jobs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


class SupervisorResultInventory:
    """Record every collected case and its terminal pytest outcome."""

    def __init__(self, output: Path, allowed_skips: dict[str, str], required_nodeids: tuple[str, ...]) -> None:
        self.output = output
        self.allowed_skips = allowed_skips
        self.required_nodeids = required_nodeids
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
        missing_selection = sorted(set(self.required_nodeids) - set(self.collected))
        missing = sorted(set(self.collected) - self.outcomes.keys())
        forbidden_skips = sorted(nodeid for nodeid, outcome in self.outcomes.items() if outcome == "forbidden-skip")
        payload = {
            "checkout_sha": os.environ.get("GITHUB_SHA", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "collected": self.collected,
            "outcomes": self.outcomes,
            "missing_selection": missing_selection,
            "missing": missing,
            "first_case_phase_epoch": self.first_phase,
            "last_case_phase_epoch": self.last_phase,
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if not self.collected or missing_selection or missing or forbidden_skips:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    """Enable the inventory only for explicitly configured CI invocations."""
    output = os.environ.get("SUPERVISOR_RESULTS_PATH")
    if output:
        config.pluginmanager.register(
            SupervisorResultInventory(
                Path(output),
                json.loads(os.environ.get("SUPERVISOR_ALLOWED_SKIPS", "{}")),
                tuple(json.loads(os.environ.get("SUPERVISOR_REQUIRED_NODEIDS", "[]"))),
            ),
            "supervisor-result-inventory",
        )
