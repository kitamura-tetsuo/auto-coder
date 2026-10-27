"""A real management-bridge child for held-drain supervisor integration tests.

This deliberately has no GitHub, provider, or issue scheduler side effects.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from auto_coder.automation_engine import EngineLifecycle
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.worker_management_bridge import BridgeIdentity, WorkerManagementBridge


class _Engine:
    def __init__(self) -> None:
        self.lifecycle = EngineLifecycle.RUNNING
        self.startup_reconciled = True
        self.startup_reconciliation_error = None
        self.daemon_serving = True
        self.gate = InvocationAdmissionGate("supervisor-fixture")

    def invocation_admission_snapshot(self):
        return self.gate.snapshot()


async def run() -> None:
    root = Path(os.environ["AUTO_CODER_RUNTIME_ROOT"])
    identity = BridgeIdentity(
        os.environ["AUTO_CODER_REPOSITORY"],
        os.environ["AUTO_CODER_CHANNEL"],
        os.environ["AUTO_CODER_DEPLOYMENT_GENERATION"],
        Path(os.environ["AUTO_CODER_MANAGEMENT_SOCKET"]),
        root,
        "fixture-digest",
    )
    engine = _Engine()
    bridge = WorkerManagementBridge(identity, engine)
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_requested.set)
    await bridge.start()
    try:
        await stop_requested.wait()
        engine.lifecycle = EngineLifecycle.DRAINING
        engine.gate.close_admission("supervisor SIGTERM")
        (root / "fixture-draining").write_text("draining")
        while not (root / "fixture-release").exists():
            await asyncio.sleep(0.02)
        engine.lifecycle = EngineLifecycle.STOPPED
        bridge.persist_graceful_terminal("supervisor SIGTERM")
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(run())
