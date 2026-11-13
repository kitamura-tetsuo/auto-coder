"""Real durable-phase emitters; no grant, operation, or readiness evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from auto_coder.management import recovery_epoch_state as epoch

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")

_EMIT = r"""
import json,os,sys
from dataclasses import asdict
from pathlib import Path
from auto_coder.logger_config import setup_logger
from auto_coder.management import recovery_epoch_state as epoch
from auto_coder.management.original_attempts import canonical
root=Path(sys.argv[1])/'epoch'
root.mkdir(mode=0o700)
fd=os.open(root/'epoch.lock',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
os.close(fd)
directory,lock=root.stat(),(root/'epoch.lock').stat()
inventory=epoch.EpochInventory(1,str(root),directory.st_dev,directory.st_ino,lock.st_dev,lock.st_ino,'component-deployment','component-authorization',('component-enrollment',),'component-request','component-preparation','a'*64,'b'*64,'c'*64)
fd=os.open(root/'inventory.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'wb') as stream:
    stream.write(canonical(asdict(inventory)))
boot,pid,start=epoch.issuer_lifetime()
binding=epoch.EpochBinding(inventory.deployment_id,inventory.authorization_id,inventory.enrollment_ids,inventory.request_id,inventory.preparation_id,inventory.target_id,inventory.source_sha256,inventory.config_sha256,'d'*64,boot,pid,start)
setup_logger(stream=sys.stdout,log_file=str(root.parent/'events.log'))
sink=epoch.logger.add(root.parent/'events.jsonl',serialize=True,rotation='1 MB',enqueue=False,diagnose=False,filter=lambda record:'recovery_epoch_phase' in record['extra'])
try:
    with epoch._hold_recovery_epoch(root,expected_uid=os.geteuid()) as state:
        state.reserve(binding)
        state.record_intent(binding)
        receipt=state.consume(binding)
    epoch.logger.complete()
finally:
    epoch.logger.remove(sink)
print(json.dumps(asdict(receipt),sort_keys=True),flush=True)
"""


def test_actual_durable_phase_logs_reach_stdout_and_file_without_success_or_readiness(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-B", "-c", _EMIT, str(tmp_path)], text=True, capture_output=True, check=True, timeout=20)
    emitted = [json.loads(line)["record"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    phases = ("reserved", "intent", "consumed", "receipt")
    assert [record["extra"]["recovery_epoch_phase"] for record in emitted] == list(phases)
    persisted_text = (tmp_path / "events.log").read_text()
    for phase, record in zip(phases, emitted):
        durable = json.loads((tmp_path / "epoch" / (phase + ".json")).read_text())
        assert durable["phase"] == phase and durable["binding"]["request_id"] == "component-request"
        assert record["file"]["path"] == str(Path(epoch.__file__).resolve())
        assert record["file"]["name"] == "recovery_epoch_state.py" and record["function"] == "_append" and record["line"] > 0
        assert record["level"]["name"] == "INFO"
        extra = record["extra"]
        assert extra["request_id"] == "component-request" and extra["target_id"] == "a" * 64
        assert extra["grant_delivered"] is False
        assert not {"operation_id", "event_type", "status", "outcome", "application_ready", "current_ready", "ready"} & set(extra)
        expected_message = f"Recovery epoch {phase} durably recorded; no grant delivery observed"
        assert record["message"] == expected_message
        assert expected_message in result.stdout and expected_message in persisted_text
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["grant_delivered"] is False
    assert set(receipt) == {"inventory_sha256", "binding_sha256", "consumed_sha256", "receipt_sha256", "grant_delivered"}
    assert os.stat(tmp_path / "epoch" / "receipt.json").st_size > 0
