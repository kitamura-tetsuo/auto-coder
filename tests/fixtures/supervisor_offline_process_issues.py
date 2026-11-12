"""Exec the real process-issues CLI without requiring live GitHub credentials.

The Supervisor invokes this fixture using the selected runtime's ``python``
from PATH, so a staged worker still runs in its installed virtual environment.
Production CLI options are used. Explicit external fault modes can interrupt one
startup attempt or hold an already produced terminal; neither supplies runtime
observations, acceptance facts, readiness, nor successful receipts.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def activate_scoped_fixture_token(token_path: Path, repository: str) -> bool:
    """Load only an explicitly mounted test-repository token into the child environment."""
    if not token_path.exists() and not token_path.is_symlink():
        return False
    if repository != "kitamura-tetsuo/auto-coder-test":
        raise ValueError("Scoped fixture token is restricted to the test repository")
    token_stat = token_path.lstat()
    if not token_path.is_file() or token_path.is_symlink() or token_stat.st_mode & 0o077:
        raise ValueError("Scoped fixture token file is not a private regular file")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token.startswith("github_pat_"):
        raise ValueError("Scoped fixture token must be a fine-grained PAT")
    os.environ["GITHUB_TOKEN"] = token
    return True


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
    activate_scoped_fixture_token(Path("/fixture/secrets/github-test-scoped"), os.environ["AUTO_CODER_REPOSITORY"])
    command = [
        sys.executable,
        "-m",
        "auto_coder.cli",
        "process-issues",
        "--repo",
        os.environ["AUTO_CODER_REPOSITORY"],
        "--skip-codex-auth-secret-sync",
        "--disable-webhook",
        "--log-file",
        os.path.join(os.environ["AUTO_CODER_RUNTIME_ROOT"], "logs", "auto-coder.log"),
    ]
    run_in_process = False
    if os.environ.get("AUTO_CODER_FIXTURE_HOLD_CANDIDATE_RUNTIME") == os.environ.get("VIRTUAL_ENV") and os.environ.get("AUTO_CODER_FIXTURE_HOLD_CANDIDATE_RUNTIME"):
        import asyncio

        from auto_coder.automation_engine import AutomationEngine

        async def hold_startup_reconciliation(engine: AutomationEngine, repository: str) -> None:
            # Delay actual startup work, not its observation. The installed
            # worker exposes its genuine unready lifecycle through the real UDS.
            root = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"])
            (root / "fixture-candidate-live-held").write_text("actual startup reconciliation held", encoding="utf-8")
            await asyncio.Event().wait()

        AutomationEngine._attempt_startup_reconciliation = hold_startup_reconciliation
        run_in_process = True
    if os.environ.get("AUTO_CODER_TEST_ABANDON_STARTUP_ONCE") == "1":
        abandoned = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"]) / "fixture-abandon-next-startup"
        if abandoned.exists():
            from auto_coder.automation_engine import AutomationEngine

            original_reconciliation = AutomationEngine._attempt_startup_reconciliation

            async def fail_one_startup_attempt(engine: AutomationEngine, repository: str) -> None:
                if abandoned.exists():
                    abandoned.unlink()
                    # External fault insertion only: the installed controller
                    # owns abandonment, the typed observation and real drain.
                    raise RuntimeError("fixture startup reconciliation abandonment")
                await original_reconciliation(engine, repository)

            AutomationEngine._attempt_startup_reconciliation = fail_one_startup_attempt
            run_in_process = True
    if os.environ.get("AUTO_CODER_TEST_HOLD_INSTALLED_TERMINAL") == "1":
        selected = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"]) / "fixture-hold-selected-runtime"
        if selected.is_file() and selected.read_text(encoding="utf-8") == os.environ.get("VIRTUAL_ENV"):
            # Execute the actual installed CLI in its selected interpreter.
            # This fault injection delays the return *after* the production
            # worker has durably persisted its genuine terminal receipt;
            # neither the receipt nor any readiness field is synthesized.
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
            run_in_process = True
    if run_in_process:
        import runpy

        sys.argv = ["auto_coder.cli", *command[3:]]
        runpy.run_module("auto_coder.cli", run_name="__main__")
        return
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
