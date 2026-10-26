"""Run the production Supervisor with a real offline process-issues child."""

from __future__ import annotations

import sys
from dataclasses import replace

from loguru import logger

from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor


def main() -> None:
    configuration = replace(
        SupervisorConfig.environment(),
        child_command=("python", "/fixtures/supervisor_offline_process_issues.py"),
    )
    (configuration.runtime_root / "supervisor").mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.remove()
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.add(sys.stdout, colorize=False, format=layout)
    logger.add(configuration.runtime_root / "supervisor" / "supervisor.log", rotation="10 MB", retention="5 days", format=layout)
    WorkerSupervisor(configuration).run()


if __name__ == "__main__":
    main()
