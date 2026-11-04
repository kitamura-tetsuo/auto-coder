"""Prepare or verify a real #2610 staged runtime in disposable CI containers."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from ci_timing import recorder

from auto_coder.worker_versions import PrepareIntent, WorkerVersionConfig, WorkerVersionPreparer


def main() -> None:
    preparer = WorkerVersionPreparer(
        WorkerVersionConfig(
            source_repository=Path("/source"),
            version_root=Path("/runtime/worker-versions"),
            python_executable=Path(sys.executable),
            uv_executable=Path("/usr/local/bin/uv"),
            uv_version="0.7.22",
        )
    )
    if len(sys.argv) == 3 and sys.argv[1] == "--verify":
        print(json.dumps(asdict(preparer.read_ready(Path(sys.argv[2]))), sort_keys=True))
        return
    if len(sys.argv) not in (2, 3):
        raise SystemExit("expected a complete checkout SHA [attempt-id] or --verify READY path")
    attempt_id = sys.argv[2] if len(sys.argv) == 3 else "supervisor-acceptance"
    timing = recorder()
    target_role = os.environ.get("AUTO_CODER_CI_TIMING_TARGET_ROLE", "supervisor-stage")
    install = preparer._install

    def timed_install(checkout: Path, environment: Path) -> None:
        lock = checkout / "uv.lock"
        lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None
        with timing.span("worker.installer", target_role=target_role, target_source_sha=sys.argv[1], lock_sha256=lock_sha256, uv_version="0.7.22"):
            install(checkout, environment)

    preparer._install = timed_install  # type: ignore[method-assign]
    with timing.span(
        "worker.prepare",
        target_role=target_role,
        target_source_sha=sys.argv[1],
        uv_version="0.7.22",
    ):
        prepared = preparer.prepare(PrepareIntent(source_sha=sys.argv[1], attempt_id=attempt_id))
    ready_path = Path(prepared.receipt.environment_path).parent / "READY.json"
    assert preparer.read_ready(ready_path) == prepared.receipt
    print(json.dumps({"ready_path": str(ready_path), "receipt": asdict(prepared.receipt)}, sort_keys=True))


if __name__ == "__main__":
    main()
