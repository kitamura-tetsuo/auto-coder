"""Native pytest phase receipts for exact-source shard qualification."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scripts.ci_shard_plugin import STATE as SHARD_STATE

from auto_coder.ci_shard_plan import _canonical, _digest
from auto_coder.source_ci_evidence import RECEIPT_SCHEMA, SOURCE_JOB, WORKFLOW_NAME, receipt_complete


@dataclass
class ReceiptState:
    path: Path
    started_at: str
    invocation_id: str
    executed: list[str] = field(default_factory=list)
    reports: list[dict[str, object]] = field(default_factory=list)


STATE: pytest.StashKey[ReceiptState | None] = pytest.StashKey()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("source-ci").addoption("--source-ci-receipt", help="Exclusive required native source-test receipt file")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--source-ci-receipt")
    config.stash[STATE] = ReceiptState(Path(path), _now(), str(uuid.uuid4())) if path else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):  # type: ignore[no-untyped-def]
    state = item.config.stash.get(STATE, None)
    if state is not None:
        state.executed.append(item.nodeid)
    yield


class PhaseCollector:
    def __init__(self, state: ReceiptState) -> None:
        self.state = state

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.state.reports.append(
            {
                "nodeid": report.nodeid,
                "phase": report.when,
                "outcome": report.outcome,
                "wasxfail": getattr(report, "wasxfail", None),
                "duration_ms": report.duration * 1000,
            }
        )


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    state = session.config.stash.get(STATE, None)
    if state is not None:
        session.config.pluginmanager.register(PhaseCollector(state), name="source-ci-native-phases")


def _publish(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise OSError("source CI receipt destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = stream.name
            stream.write(_canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    state = session.config.stash.get(STATE, None)
    if state is None:
        return
    adapter = session.config.stash.get(SHARD_STATE, None)
    if adapter is None or adapter.mode != "apply" or adapter.plan is None or adapter.shard is None:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print("Source CI receipt requires an applied immutable shard plan", file=sys.stderr)
        return
    plan = adapter.plan
    origin = plan.context.origin
    try:
        # Never relabel work from an earlier attempt or a different job.
        if os.environ.get("GITHUB_RUN_ID") != origin.get("run_id") or os.environ.get("GITHUB_RUN_ATTEMPT") != origin.get("run_attempt") or os.environ.get("GITHUB_WORKFLOW") != WORKFLOW_NAME or os.environ.get("GITHUB_JOB") != SOURCE_JOB or os.environ.get("GITHUB_SHA") != origin.get("event_sha"):
            raise ValueError("actual producing Actions identity differs from the immutable source plan")
        payload: dict[str, object] = {
            "schema_version": RECEIPT_SCHEMA,
            "repository": plan.context.repository,
            "source_sha": plan.context.checkout_sha,
            "profile": plan.profile,
            "plan_digest": plan.digest,
            "inventory_digest": _digest(list(plan.inventory)),
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "planning_attempt": origin["planning_attempt"],
            "workflow": WORKFLOW_NAME,
            "job_key": SOURCE_JOB,
            "shard": adapter.shard,
            "shard_count": len(plan.shards),
            "invocation_id": state.invocation_id,
            "started_at": state.started_at,
            "completed_at": _now(),
            "collected": list(adapter.collected),
            "assigned": list(plan.shards[adapter.shard - 1]),
            "selected": list(adapter.selected),
            "executed": state.executed,
            "reports": state.reports,
            "native_exit_status": int(session.exitstatus),
        }
        payload["complete"] = receipt_complete(payload, plan, adapter.shard)
        _publish(state.path, payload)
        if not payload["complete"]:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            print("Source CI native receipt records incomplete or failed required tests", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError) as error:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print(f"Source CI receipt failed: {error}", file=sys.stderr)
