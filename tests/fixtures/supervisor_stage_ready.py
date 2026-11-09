"""Prepare or verify a real #2610 staged runtime in disposable CI containers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from ci_timing import installer_span, recorder
from integration_dependency_observation import mark_cache_population, observe_uv_sync

from auto_coder.worker_versions import PrepareIntent, WorkerVersionConfig, WorkerVersionError, WorkerVersionPreparer


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
    execution_id = os.environ.get("AUTO_CODER_DEPENDENCY_EXECUTION_ID") or str(uuid.uuid4())
    install = preparer._install

    def timed_install(checkout: Path, environment: Path) -> None:
        with installer_span(timing, checkout, "worker.installer", target_role=target_role, target_source_sha=sys.argv[1], uv_version="0.7.22", execution_id=execution_id):
            install(checkout, environment)

    preparer._install = timed_install  # type: ignore[method-assign]
    observation_root = Path(os.environ.get("AUTO_CODER_CI_TIMING_DIR", "/runtime"))
    observation = observation_root / f"dependency-work-{target_role}-{attempt_id}.jsonl"
    with observe_uv_sync(
        preparer.config.uv_executable,
        observation,
        target_role=target_role,
        target_source_sha=sys.argv[1],
        attempt_id=attempt_id,
        execution_id=execution_id,
        python_executable=preparer.config.python_executable,
        uv_version=preparer.config.uv_version,
    ):
        with timing.span(
            "worker.prepare",
            target_role=target_role,
            target_source_sha=sys.argv[1],
            uv_version="0.7.22",
            cache_condition=os.environ.get("AUTO_CODER_DEPENDENCY_CACHE_CONDITION", "uncached"),
            execution_id=execution_id,
        ):
            try:
                prepared = preparer.prepare(PrepareIntent(source_sha=sys.argv[1], attempt_id=attempt_id))
            except WorkerVersionError as exc:
                # The production preparer intentionally redacts Git stderr, which
                # may contain credentials. The disposable fixture reports only a
                # fixed, allowlisted category for a failed local clone.
                cause = exc.__cause__
                if isinstance(cause, subprocess.CalledProcessError) and isinstance(cause.cmd, (list, tuple)) and list(cause.cmd[:2]) == ["git", "clone"]:
                    diagnostic = (cause.stderr or "").lower()
                    patterns = (
                        ("untrusted-source", ("dubious ownership", "unsafe repository")),
                        ("destination-exists", ("destination path", "already exists")),
                        ("filesystem-permission", ("permission denied", "operation not permitted")),
                        ("cross-filesystem", ("invalid cross-device link", "cross-device")),
                        ("source-invalid", ("not a git repository", "does not appear to be a git repository")),
                        ("source-inaccessible", ("cannot access", "could not read from remote repository")),
                        ("storage-full", ("no space left", "disk quota")),
                        ("missing-path", ("no such file or directory",)),
                    )
                    category = next(
                        (name for name, phrases in patterns if any(phrase in diagnostic for phrase in phrases)),
                        "other",
                    )
                    print(f"disposable-ready-local-clone-failure={category}", file=sys.stderr)
                raise
    mark_cache_population(
        prepared.receipt.source_sha,
        prepared.receipt.lock_sha256,
        target_role=target_role,
        attempt_id=attempt_id,
    )
    ready_path = Path(prepared.receipt.environment_path).parent / "READY.json"
    assert preparer.read_ready(ready_path) == prepared.receipt
    print(json.dumps({"ready_path": str(ready_path), "receipt": asdict(prepared.receipt)}, sort_keys=True))


if __name__ == "__main__":
    main()
