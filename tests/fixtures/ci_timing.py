"""Non-authorizing timing diagnostics for CI integration harnesses.

Every process writes its own append-only JSONL file.  Recording is deliberately
best effort: a broken diagnostic destination must never change the operation
being observed.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from loguru import logger

SCHEMA_VERSION = 1
ENVIRONMENT_VARIABLE = "AUTO_CODER_CI_TIMING_DIR"


@dataclass(frozen=True)
class TimingContext:
    checkout_sha: str | None
    event_sha: str | None
    workflow: str | None
    run_id: str | None
    run_attempt: str | None
    job_key: str | None
    invocation_id: str
    process_id: str
    pid: int
    python_version: str
    comparison_case: str | None = None


def _checkout_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


class TimingRecorder:
    """A process-local, failure-isolated JSONL event recorder."""

    def __init__(self, directory: Path | None = None) -> None:
        configured = os.environ.get(ENVIRONMENT_VARIABLE)
        self.directory = directory or (Path(configured) if configured else None)
        self.context = TimingContext(
            checkout_sha=os.environ.get("AUTO_CODER_CI_TIMING_CHECKOUT_SHA") or _checkout_sha(),
            event_sha=os.environ.get("GITHUB_SHA"),
            workflow=os.environ.get("GITHUB_WORKFLOW"),
            run_id=os.environ.get("GITHUB_RUN_ID"),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
            job_key=os.environ.get("GITHUB_JOB"),
            invocation_id=os.environ.get("AUTO_CODER_CI_TIMING_INVOCATION") or str(uuid.uuid4()),
            process_id=str(uuid.uuid4()),
            pid=os.getpid(),
            python_version=platform.python_version(),
            comparison_case=os.environ.get("AUTO_CODER_CI_TIMING_CASE"),
        )
        self.path = self.directory / f"timing-{self.context.invocation_id}-{self.context.process_id}.jsonl" if self.directory else None
        self.last_error: str | None = None
        self._error_reported = False
        self._parents: list[str] = []

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _report_error(self) -> None:
        """Emit one protocol-safe warning without taking ownership of sinks."""

        if self._error_reported or self.last_error is None:
            return
        sink_id: int | None = None
        try:
            sink_id = logger.add(
                sys.__stderr__,
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}",
                filter=lambda record: bool(record["extra"].get("ci_timing_direct")),
                catch=True,
            )
            logger.bind(ci_timing_direct=True).warning("CI timing diagnostic unavailable: {}", self.last_error)
        except (OSError, ValueError):
            pass
        finally:
            if sink_id is not None:
                try:
                    logger.remove(sink_id)
                except ValueError:
                    pass
            self._error_reported = True

    def emit(self, event_kind: str, **values: object) -> bool:
        if self.path is None:
            return False
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_kind": event_kind,
            "context": asdict(self.context),
            **values,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True, default=str) + "\n")
                stream.flush()
            return True
        except (OSError, TypeError, ValueError) as error:
            self.last_error = f"{type(error).__name__}: {error}"
            self._report_error()
            return False

    @contextmanager
    def span(self, label: str, **metadata: object) -> Iterator[str]:
        span_id = str(uuid.uuid4())
        active = _ACTIVE_PYTEST.get()
        parent = self._parents[-1] if self._parents else active[1]
        started = time.perf_counter_ns()
        common = {
            "span_id": span_id,
            "parent_span_id": parent,
            "phase_label": label,
            "nodeid": active[0],
            **metadata,
        }
        self.emit(
            "span_start",
            **common,
            started_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        self._parents.append(span_id)
        outcome = "completed"
        try:
            yield span_id
        except BaseException as error:
            outcome = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "exception"
            raise
        finally:
            self._parents.pop()
            self.emit(
                "span_end",
                **common,
                elapsed_ms=(time.perf_counter_ns() - started) / 1_000_000,
                outcome=outcome,
            )


_RECORDER: TimingRecorder | None = None
_ACTIVE_PYTEST: ContextVar[tuple[str | None, str | None]] = ContextVar("ci_timing_active_pytest", default=(os.environ.get("AUTO_CODER_CI_TIMING_NODEID"), os.environ.get("AUTO_CODER_CI_TIMING_PARENT_SPAN")))


def recorder() -> TimingRecorder:
    global _RECORDER
    if _RECORDER is None:
        _RECORDER = TimingRecorder()
    return _RECORDER


@contextmanager
def pytest_phase_context(nodeid: str, span_id: str) -> Iterator[None]:
    """Bind harness spans created during a pytest phase to that exact phase."""

    token = _ACTIVE_PYTEST.set((nodeid, span_id))
    try:
        yield
    finally:
        _ACTIVE_PYTEST.reset(token)


def docker_timing_arguments(container_directory: str = "/ci-timing", *, target_role: str | None = None) -> list[str]:
    """Return explicit Docker propagation for an enabled host recorder."""

    timing = recorder()
    if not timing.enabled or timing.directory is None:
        return []
    try:
        timing.directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        timing.last_error = f"{type(error).__name__}: {error}"
        timing._report_error()
        return []
    active_nodeid, active_parent = _ACTIVE_PYTEST.get()
    environment = {
        "AUTO_CODER_CI_TIMING_DIR": container_directory,
        "AUTO_CODER_CI_TIMING_INVOCATION": timing.context.invocation_id,
        "AUTO_CODER_CI_TIMING_CHECKOUT_SHA": timing.context.checkout_sha,
        "AUTO_CODER_CI_TIMING_NODEID": active_nodeid,
        "AUTO_CODER_CI_TIMING_PARENT_SPAN": active_parent,
        "GITHUB_SHA": timing.context.event_sha,
        "GITHUB_WORKFLOW": timing.context.workflow,
        "GITHUB_RUN_ID": timing.context.run_id,
        "GITHUB_RUN_ATTEMPT": timing.context.run_attempt,
        "GITHUB_JOB": timing.context.job_key,
        "AUTO_CODER_CI_TIMING_TARGET_ROLE": target_role,
    }
    arguments = ["-v", f"{timing.directory.resolve()}:{container_directory}"]
    for key, value in environment.items():
        if value is not None:
            arguments.extend(["-e", f"{key}={value}"])
    return arguments


@dataclass(frozen=True)
class NestedInstallEvidence:
    complete: bool
    labels: tuple[str, ...]
    errors: tuple[str, ...]


def observe_nested_install_evidence(target_role: str, source_sha: str) -> NestedInstallEvidence:
    """Inspect nested evidence without making it an acceptance prerequisite."""

    timing = recorder()
    if not timing.enabled or timing.directory is None:
        return NestedInstallEvidence(False, (), ("timing is disabled",))
    events: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        paths = list(timing.directory.glob("timing-*.jsonl"))
    except OSError as error:
        paths = []
        errors.append(f"{type(error).__name__}: {error}")
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            errors.append(f"{path.name}: {type(error).__name__}: {error}")
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(f"{path.name}: invalid JSON at {error.lineno}:{error.colno}")
                continue
            if not isinstance(event, dict):
                errors.append(f"{path.name}: timing record is not an object")
                continue
            if event.get("event_kind") == "span_end" and event.get("target_role") == target_role and event.get("target_source_sha") == source_sha:
                events.append(event)
    labels = tuple(sorted(str(event.get("phase_label")) for event in events))
    complete = {"worker.prepare", "worker.installer"} <= set(labels)
    if not complete:
        errors.append(f"missing nested prepare/installer evidence for {target_role} {source_sha}")
    if errors:
        timing.last_error = "; ".join(errors)
        timing._report_error()
    return NestedInstallEvidence(complete, labels, tuple(errors))
