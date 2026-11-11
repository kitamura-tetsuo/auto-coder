"""Run the production Supervisor with a real offline process-issues child."""

from __future__ import annotations

import inspect
import os
import signal
import sys
import threading
from dataclasses import replace

from loguru import logger

from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor


class _FixtureReconstructSupervisor(Exception):
    """Stop only the disposable Supervisor listener, retaining its worker."""


def main() -> None:
    configuration = replace(
        SupervisorConfig.environment(),
        child_command=("python", "/fixtures/supervisor_offline_process_issues.py"),
    )
    (configuration.runtime_root / "supervisor").mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.environ.get("AUTO_CODER_TEST_REQUEST_AUDIT") == "1":
        # Audit actual signed bridge POST effects, not synthetic heartbeats.
        from auto_coder.worker_supervisor_bridge import _BridgeHandler

        original = _BridgeHandler.do_POST

        def audited(handler):
            descriptor = os.open(
                configuration.runtime_root / "fixture-posts",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write("post\n")
                stream.flush()
                os.fsync(stream.fileno())
            return original(handler)

        _BridgeHandler.do_POST = audited
    logger.remove()
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.add(sys.stdout, colorize=False, format=layout)
    logger.add(configuration.runtime_root / "supervisor" / "supervisor.log", rotation="10 MB", retention="5 days", format=layout)
    if os.environ.get("AUTO_CODER_TEST_RECONSTRUCT_SUPERVISOR") != "1":
        WorkerSupervisor(configuration).run()
        return

    def reconstruct(_signal_number: int, _frame: object) -> None:
        raise _FixtureReconstructSupervisor()

    if os.environ.get("AUTO_CODER_TEST_INTERRUPT_REBOOT_EXIT") == "1":
        # Inject an actual owner interruption at the last pre-exit boundary.
        # Production persists and validates all reboot evidence itself. No
        # lifetime observation, durable state, or success result is replaced.
        source, first_line = inspect.getsourcelines(WorkerSupervisor._exit_for_reboot_if_eligible)
        exit_line = first_line + next(index for index, line in enumerate(source) if line.strip() == "os._exit(81)")

        def interrupt_exit(frame, event, argument):
            if event == "line" and frame.f_code is WorkerSupervisor._exit_for_reboot_if_eligible.__code__ and frame.f_lineno == exit_line:
                sys.settrace(None)
                if os.environ.get("AUTO_CODER_TEST_EXIT_CANDIDATE_BEFORE_RECONSTRUCTION") == "1":
                    owner = frame.f_locals["self"]
                    candidate = owner.store.read()["worker"]
                    os.kill(candidate["pid"], signal.SIGKILL)
                    os.waitpid(candidate["pid"], 0)
                raise _FixtureReconstructSupervisor()
            return interrupt_exit

        sys.settrace(interrupt_exit)

    signal.signal(signal.SIGUSR1, reconstruct)
    reconstructions = 0
    while True:
        try:
            WorkerSupervisor(configuration).run()
            return
        except _FixtureReconstructSupervisor:
            if os.environ.get("AUTO_CODER_TEST_INTERRUPT_REBOOT_EXIT") == "1":
                # Listener close leaves daemon request threads alive. Retire
                # the old owner's real requests before constructing its
                # replacement; they must not write a stale pre-refusal snapshot.
                retired = [thread for thread in threading.enumerate() if "process_request_thread" in thread.name]
                for thread in retired:
                    thread.join(timeout=10)
                    if thread.is_alive():
                        raise RuntimeError("Retired Supervisor request did not finish")
            # This is an out-of-band fault injection, not a management action.
            # The real Supervisor closes the old signed listener and releases
            # ownership; its separately running child process is not stopped.
            reconstructions += 1
            (configuration.runtime_root / "fixture-supervisor-reconstructions").write_text(str(reconstructions), encoding="utf-8")
            logger.info("Fixture reconstructed Supervisor without stopping worker")


if __name__ == "__main__":
    main()
