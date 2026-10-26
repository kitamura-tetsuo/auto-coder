"""Launch the production supervisor with one fixed, harmless integration child."""

from __future__ import annotations

import sys
from dataclasses import replace

from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor


def main() -> None:
    config = SupervisorConfig.environment()
    fixture = "/fixtures/supervisor_bridge_worker.py"
    WorkerSupervisor(replace(config, child_command=(sys.executable, fixture))).run()


if __name__ == "__main__":
    main()
