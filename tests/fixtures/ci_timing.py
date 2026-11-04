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
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
ENVIRONMENT_VARIABLE = "AUTO_CODER_CI_TIMING_DIR"


@dataclass(frozen=True)
class TimingContext:
    checkout_sha: str | None
    workflow: str | None
    run_id: str | None
    run_attempt: str | None
    job_key: str | None
    invocation_id: str
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
            checkout_sha=os.environ.get("GITHUB_SHA") or _checkout_sha(),
            workflow=os.environ.get("GITHUB_WORKFLOW"),
            run_id=os.environ.get("GITHUB_RUN_ID"),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
            job_key=os.environ.get("GITHUB_JOB"),
            invocation_id=os.environ.get("AUTO_CODER_CI_TIMING_INVOCATION") or str(uuid.uuid4()),
            pid=os.getpid(),
            python_version=platform.python_version(),
            comparison_case=os.environ.get("AUTO_CODER_CI_TIMING_CASE"),
        )
        self.path = self.directory / f"timing-{self.context.invocation_id}-{self.context.pid}.jsonl" if self.directory else None
        self.last_error: str | None = None
        self._parents: list[str] = []

    @property
    def enabled(self) -> bool:
        return self.path is not None

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
            return False

    @contextmanager
    def span(self, label: str, **metadata: object) -> Iterator[str]:
        span_id = str(uuid.uuid4())
        parent = self._parents[-1] if self._parents else None
        started = time.perf_counter_ns()
        common = {
            "span_id": span_id,
            "parent_span_id": parent,
            "phase_label": label,
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


def recorder() -> TimingRecorder:
    global _RECORDER
    if _RECORDER is None:
        _RECORDER = TimingRecorder()
    return _RECORDER
