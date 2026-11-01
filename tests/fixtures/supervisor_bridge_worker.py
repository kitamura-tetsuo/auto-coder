"""A real management-bridge child for held-drain supervisor integration tests.

This deliberately has no GitHub, provider, or issue scheduler side effects.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
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
    if os.environ.get("AUTO_CODER_TEST_CONFIG_REPORT") == "1":
        from auto_coder.automation_config import AutomationConfig

        config = AutomationConfig(repo_name=os.environ["AUTO_CODER_REPOSITORY"])
        (root / "fixture-config-observed.json").write_text(json.dumps({"issue": config.issue_allowlist, "pr": config.pr_allowlist}))
    identity = BridgeIdentity(
        os.environ["AUTO_CODER_REPOSITORY"],
        os.environ["AUTO_CODER_CHANNEL"],
        os.environ["AUTO_CODER_DEPLOYMENT_GENERATION"],
        Path(os.environ["AUTO_CODER_MANAGEMENT_SOCKET"]),
        root,
        "fixture-digest",
    )
    engine = _Engine()
    test_package = os.environ.get("AUTO_CODER_TEST_CONSUMER_PACKAGE")
    if test_package:
        import auto_coder.consumer_identity as consumer_identity

        consumer_identity._PACKAGE = Path(test_package)
    bridge = WorkerManagementBridge(identity, engine)
    # Hold an actual worker-produced UDS response, not a fabricated network
    # response, so the private Supervisor transport's timeout is exercised.
    original_observation = bridge._observation

    def optionally_slow_observation(nonce: str) -> dict[str, object]:
        if (root / "fixture-hold-observation").exists():
            (root / "fixture-observation-entered").write_text("entered")
            while (root / "fixture-hold-observation").exists():
                time.sleep(0.03)
        return original_observation(nonce)

    bridge._observation = optionally_slow_observation  # type: ignore[method-assign]
    original_consumer_observe = bridge.consumer_identity.observe

    def optionally_slow_consumer_observe():
        if (root / "fixture-hold-consumer").exists():
            (root / "fixture-consumer-entered").write_text("entered")
            while (root / "fixture-hold-consumer").exists():
                time.sleep(0.03)
        return original_consumer_observe()

    bridge.consumer_identity.observe = optionally_slow_consumer_observe  # type: ignore[method-assign]
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
