"""Synthetic normal-owner close-fault process; startup/readiness is controlled.

Canonical main/run/_run_owned, Popen, ledger and kernel locks are real. The
already-initialized readiness port is controlled without fabricating receipts.
No Docker, installed-runtime conformance or live state is used.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from auto_coder.management import bootstrap_supervisor, retained_startup
from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.recovery_mailbox import RecoveryIntent
from auto_coder.management.recovery_supervisor import RecoveryConsumer
from tests.fixtures.fresh_launch_process import CHILD
from tests.test_management_fresh_launch_exclusions import first_launch_case


def main():
    root, boundary, restart, close_order = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "true", sys.argv[4]
    dispatch_signal = None if len(sys.argv) <= 5 or sys.argv[5] == "none" else int(sys.argv[5])
    root.mkdir(exist_ok=True)
    patches = pytest.MonkeyPatch()
    fixture = first_launch_case.__wrapped__(root, patches, N(param="clean-restart" if restart else None))
    case = next(fixture)
    case.owner.close()  # The canonical CLI must obtain its own real EX lock.
    original_reader = retained_startup.retained_from_fresh_intent

    class CloseFailure:
        def __init__(self, reader):
            self.reader = reader

        def read(self):
            return self.reader.read()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            if close_order == "after-close":
                self.reader.close()
            raise OSError("synthetic temporary reader close failure")

    patches.setattr(retained_startup, "retained_from_fresh_intent", lambda *args, **kwargs: CloseFailure(original_reader(*args, **kwargs)))
    real_popen = subprocess.Popen

    def physical(_argv, **kwargs):
        child = real_popen([sys.executable, "-I", "-c", CHILD, str(root), "close-after-popen"], **kwargs)
        deadline = time.monotonic() + 10
        while True:
            if (root / "child-ready").exists():
                try:
                    identity = json.loads((root / "child.json").read_text())
                except json.JSONDecodeError:
                    identity = None
                if identity is not None and identity["pid"] == child.pid:
                    break
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        with (root / "spawned.jsonl").open("a") as stream:
            stream.write(json.dumps(identity) + "\n")
        if dispatch_signal is not None:
            os.kill(os.getpid(), dispatch_signal)
        return child

    patches.setattr(bootstrap_supervisor, "subprocess", N(Popen=physical, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError))
    # Recovery imports the same standard subprocess module; replace only its
    # physical runtime port, preserving the independent test controller's Popen.
    patches.setattr("auto_coder.management.recovery_supervisor.subprocess", N(Popen=physical, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError))
    original_owned = bootstrap_supervisor.ManagementBootstrapSupervisor._run_owned

    def normal_owner(self):
        self._fresh_observer = N(succeeded=True, tick=lambda: None)
        self._acquire_prior_fences(case.intent)
        self.store.publish_supervisor(self.incarnation, os.getpid(), self._start_token(os.getpid()))
        runtime = self._runtime("bootstrap")
        original = ChildRecord(self.config.instance_id, ChildPhase.STOPPED, "bootstrap", child_generation=1, supervisor_incarnation=self.incarnation, intended_stop=True, runtime_receipt=runtime.receipt)
        if boundary == "restore":
            original = ChildRecord(self.config.instance_id, ChildPhase.NEEDS_ATTENTION, "failed-candidate", previous_runtime="bootstrap", child_generation=1, supervisor_incarnation=self.incarnation, effect_operation_id="close-start", effect_attempt_id="attempt")
        self.store.save(original)
        if boundary == "restore":
            self.store.begin_effect("close-start", "attempt", "START_SELECTED", "failed-candidate", "")
            result = self._restore_once("close-start", original)
            status = result.status.value
        elif boundary == "recovery":
            consumer = RecoveryConsumer(self, self.bootstrap_config_path)
            config = self.config.management_config
            intent = RecoveryIntent(str(uuid.uuid4()), self.config.instance_id, self.incarnation, 1, "bootstrap", runtime.receipt, runtime.source_receipt, str(config), hashlib.sha256(config.read_bytes()).hexdigest())
            consumer.mailbox.submit(intent)
            assert consumer._set(intent.request_id, "pending", "claimed", "synthetic recovery admission")
            patches.setattr(consumer, "_settings", lambda *_: N())
            consumer._launch(intent, original, runtime)
            status = consumer.mailbox.get(intent.request_id).status
        else:
            result = self.start_selected("close-start", "attempt", "bootstrap")
            status = result.status.value
        (root / "start-result.json").write_text(json.dumps({"status": status, "fresh": self._fresh_launch_may_exist, "owned": len(self._children), "child": asdict(self.store.load())}))
        (root / "start-returned").touch()
        return original_owned(self)

    patches.setattr(bootstrap_supervisor.ManagementBootstrapSupervisor, "_run_owned", normal_owner)
    sys.argv = ["bootstrap-supervisor", "--config", str(case.supervisor.bootstrap_config_path)]
    try:
        bootstrap_supervisor.main()
    finally:
        fixture.close()
        patches.undo()


if __name__ == "__main__":
    main()
