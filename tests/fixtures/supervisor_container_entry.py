"""Launch the production supervisor with one fixed, harmless integration child."""

from __future__ import annotations

import os
from dataclasses import replace

from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor


def main() -> None:
    config = SupervisorConfig.environment()
    if os.environ.get("AUTO_CODER_TEST_REQUEST_AUDIT") == "1":
        # Observe actual incoming POSTs without replacing transport or decisions.
        from auto_coder.worker_supervisor_bridge import _BridgeHandler

        original = _BridgeHandler.do_POST

        def audited(handler):
            descriptor = os.open(config.runtime_root / "fixture-posts", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write("post\n")
                stream.flush()
                os.fsync(stream.fileno())
            return original(handler)

        _BridgeHandler.do_POST = audited
    fixture = "/fixtures/supervisor_bridge_worker.py"
    WorkerSupervisor(replace(config, child_command=("python", fixture))).run()


if __name__ == "__main__":
    main()
