"""Prepare or verify a real #2610 staged runtime in disposable CI containers."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

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
    prepared = preparer.prepare(PrepareIntent(source_sha=sys.argv[1], attempt_id=attempt_id))
    ready_path = Path(prepared.receipt.environment_path).parent / "READY.json"
    assert preparer.read_ready(ready_path) == prepared.receipt
    print(json.dumps({"ready_path": str(ready_path), "receipt": asdict(prepared.receipt)}, sort_keys=True))


if __name__ == "__main__":
    main()
