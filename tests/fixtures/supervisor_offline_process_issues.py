"""Exec the real process-issues CLI without requiring live GitHub credentials.

The Supervisor invokes this fixture using the selected runtime's ``python``
from PATH, so a staged worker still runs in its installed virtual environment.
Only production CLI options are used; no application functions are replaced.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    if os.environ.get("AUTO_CODER_TEST_LAUNCH_AUDIT") == "1":
        # External acceptance counts actual selected-interpreter processes.
        # This records launch facts only, never readiness or drain evidence.
        audit = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"]) / "fixture-launches.jsonl"
        with audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "start_token": Path("/proc/self/stat").read_text().rsplit(") ", 1)[1].split()[19], "deployment_generation": os.environ["AUTO_CODER_DEPLOYMENT_GENERATION"], "interpreter": sys.executable, "environment": os.environ.get("VIRTUAL_ENV")}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    if os.environ.get("AUTO_CODER_FIXTURE_HOLD_BOOTSTRAP") == "1" and "/worker-versions/" not in os.environ.get("VIRTUAL_ENV", ""):
        # The disposable bootstrap incumbent holds a genuine UDS/OS drain;
        # the subsequently selected locked-uv runtime still executes the
        # unmodified production process-issues CLI below.
        os.execv(sys.executable, [sys.executable, "/fixtures/supervisor_bridge_worker.py"])
    if os.environ.get("AUTO_CODER_FIXTURE_HOLD_CANDIDATE_RUNTIME") == os.environ.get("VIRTUAL_ENV") and os.environ.get("AUTO_CODER_FIXTURE_HOLD_CANDIDATE_RUNTIME"):
        # Hold a genuine selected candidate OS lifetime before it can serve
        # readiness. Only the outer Docker restart can release this process.
        root = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"])
        (root / "fixture-candidate-live-held").write_text("selected candidate alive without readiness", encoding="utf-8")
        while True:
            time.sleep(0.1)
    if os.environ.get("AUTO_CODER_FIXTURE_FAIL_STAGED_ONCE") == "1" and "/worker-versions/" in os.environ.get("VIRTUAL_ENV", ""):
        # Crash one genuine Supervisor-launched staged process before it can
        # publish a producer heartbeat; a recovered previous runtime must
        # execute the unmodified production CLI on its next start.
        crash = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"]) / "fixture-crash-next-staged"
        if crash.exists():
            crash.unlink()
            # Ensure the real Supervisor records the child's PID/start token
            # before the fixture process exits unexpectedly.
            time.sleep(0.75)
            raise SystemExit("fixture intentional staged-worker activation failure")
    token = os.environ["GITHUB_TOKEN"]
    command = [
        sys.executable,
        "-m",
        "auto_coder.cli",
        "process-issues",
        "--repo",
        os.environ["AUTO_CODER_REPOSITORY"],
        "--github-token",
        token,
        "--skip-codex-auth-secret-sync",
        "--disable-webhook",
        "--log-file",
        os.path.join(os.environ["AUTO_CODER_RUNTIME_ROOT"], "logs", "auto-coder.log"),
    ]
    if os.environ.get("AUTO_CODER_TEST_HOLD_INSTALLED_TERMINAL") == "1":
        selected = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"]) / "fixture-hold-selected-runtime"
        if selected.is_file() and selected.read_text(encoding="utf-8") == os.environ.get("VIRTUAL_ENV"):
            # Execute the actual installed CLI in its selected interpreter.
            # This fault injection delays the return *after* the production
            # worker has durably persisted its genuine terminal receipt;
            # neither the receipt nor any readiness field is synthesized.
            import runpy

            from auto_coder.worker_management_bridge import WorkerManagementBridge

            original_terminal = WorkerManagementBridge.persist_graceful_terminal

            def persist_then_hold(bridge: WorkerManagementBridge, reason: str) -> None:
                original_terminal(bridge, reason)
                root = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"])
                hold = root / "fixture-hold-terminal-exit"
                if hold.exists():
                    (root / "fixture-terminal-exit-held").write_text("genuine terminal persisted, exact OS lifetime held", encoding="utf-8")
                    while hold.exists():
                        time.sleep(0.02)

            WorkerManagementBridge.persist_graceful_terminal = persist_then_hold
            sys.argv = ["auto_coder.cli", *command[3:]]
            runpy.run_module("auto_coder.cli", run_name="__main__")
            return
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
