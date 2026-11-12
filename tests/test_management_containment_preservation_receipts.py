"""Receipt regressions with real files and children, not full-host acceptance.

The CLI adapter uses small controlled child programs. These tests do not supply
synthetic reports to the maintained native acceptance driver.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import test_management_containment_closed_host as fixture

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")

_REPORT_CHILD = """import json,os,sys,time,uuid
report=json.loads(sys.argv[1])
started=time.monotonic_ns()
report.update(observer_pid=os.getpid(),observer_start_token=open('/proc/self/stat').read().rsplit(')',1)[1].split()[19],observation_id=uuid.uuid4().hex,started_monotonic_ns=started,finished_monotonic_ns=time.monotonic_ns())
print(json.dumps(report))
raise SystemExit(int(sys.argv[2]))
"""
_RACE_CHILD = """import json,os,sys,time,uuid
started=time.monotonic_ns()
path=sys.argv[3]
with open(path,'rb') as stream: original=stream.read()
while True:
    with open(path,'rb') as stream: current=stream.read()
    if current != original: break
    time.sleep(0.01)
report=json.loads(sys.argv[1])
report.update(observer_pid=os.getpid(),observer_start_token=open('/proc/self/stat').read().rsplit(')',1)[1].split()[19],observation_id=uuid.uuid4().hex,started_monotonic_ns=started,finished_monotonic_ns=time.monotonic_ns())
print(json.dumps(report))
raise SystemExit(int(sys.argv[2]))
"""


def _report(*, accepted: bool) -> dict[str, object]:
    if not accepted:
        return {"status": "unavailable", "unavailable": [{"authority": "approved-enrollment-component", "reason": "controlled receipt boundary"}]}
    return {
        "status": fixture.OBSERVED,
        "unavailable": [],
        "approved_enrollment": {},
        "watcher_systemd": {},
        "installed_bootstrap_source": {"reviewed_lock_before_launch": True, "source_sha256": fixture._REVIEWED_ORDINARY_SOURCE_SHA256},
        "installed_mount_profile": {"profile": "ordinary-v1"},
        "route_manifest": {"closed_profile": True},
        "kernel_subobservation": {"original_cgroups_empty": True, "unpinned_network_task_scan_absent": True, "host_listener_absent": True, "external_namespace_pin_verified": True, "external_owner_fence_verified": True, "retained_original_network_pin": False},
    }


@pytest.fixture
def receipt_case(tmp_path: Path):
    protected = tmp_path / "protected"
    protected.mkdir()
    (tmp_path / "evidence").mkdir()
    config = protected / "config.json"
    config.write_bytes(b'{"private":"CONFIG_SECRET_BODY"}')
    management = protected / "management.yml"
    management.write_bytes(b"private: MANAGEMENT_SECRET_BODY\n")
    oauth = protected / "oauth.sqlite"
    oauth.write_bytes(b"OAUTH_SECRET_BODY")
    worker_config = protected / "worker.json"
    worker_config.write_bytes(b'{"worker":"unchanged"}')
    (protected / "link").symlink_to("PRIVATE_LINK_TARGET")
    scope = protected / "scope.json"
    scope.write_text("{}")
    previous = protected / "previous.json"
    previous.write_text("{}")
    worker = subprocess.Popen((sys.executable, "-I", "-B", "-c", "import signal; signal.pause()"), text=True)
    case = fixture.Case(tmp_path, "ac-test-receipt", "ordinary-v1", "manager", "sha256:" + "a" * 64, config, management, protected, oauth, SimpleNamespace(), -1, SimpleNamespace(), worker, worker_config, protected=(protected,), scope=scope, previous=previous)
    try:
        yield case
    finally:
        if worker.poll() is None:
            worker.terminate()
        worker.wait(timeout=5)


def _receipt(case, label: str, phase: str) -> dict[str, object]:
    path = case.root / "evidence" / (label + ".preservation-" + phase + ".json")
    assert path in case.artifacts
    assert path.stat().st_mode & 0o777 == 0o600
    return json.loads(path.read_text())


def _spawn_report(real_popen, kwargs, *, accepted: bool):
    return real_popen((sys.executable, "-I", "-B", "-c", _REPORT_CHILD, json.dumps(_report(accepted=accepted)), "0" if accepted else "2"), **kwargs)


@pytest.mark.parametrize("verify", (False, True))
def test_actual_before_receipt_is_fsynced_before_launch_and_exit_is_bound(receipt_case, monkeypatch: pytest.MonkeyPatch, verify: bool) -> None:
    case = receipt_case
    label = "ordered-verify" if verify else "ordered-observe"
    real_popen, real_fsync = subprocess.Popen, os.fsync
    synced = []
    children = []

    def fsync(fd):
        real_fsync(fd)
        synced.append(fd)

    def launch(arguments, **kwargs):
        before = _receipt(case, label, "before")
        assert synced and before["capture_error"] is None
        assert before["cli_mode"] == ("verify" if verify else "observe")
        assert arguments[5] == before["cli_mode"]
        process = _spawn_report(real_popen, kwargs, accepted=False)
        children.append(process)
        return process

    monkeypatch.setattr(fixture.os, "fsync", fsync)
    monkeypatch.setattr(fixture.subprocess, "Popen", launch)
    case.command(label, accepted=False, authority="approved-enrollment-component", verify=verify)
    before, after = _receipt(case, label, "before"), _receipt(case, label, "after")
    assert after["fingerprint"] == before["fingerprint"]
    assert after["status"] == "passed"
    assert after["unqualified_before_after_equal"] is True
    assert after["comparisons"] == [{"from": "before", "to": "after", "excluded_paths": [], "equal": True}]
    assert after["intentional_mutation"] is None
    assert after["process"]["pid"] == children[0].pid
    assert after["process"]["returncode"] == 2
    assert after["process"]["termination_signal"] is None
    assert before["finished_monotonic_ns"] <= after["process"]["spawn_returned_monotonic_ns"] < after["started_monotonic_ns"] <= after["finished_monotonic_ns"]
    assert after["fingerprint"]["worker"] == {"pid": case.worker.pid, "start_token": fixture._start_token(case.worker.pid)}
    for phase in (before, after):
        encoded = json.dumps(phase)
        for secret in ("CONFIG_SECRET_BODY", "MANAGEMENT_SECRET_BODY", "OAUTH_SECRET_BODY", "PRIVATE_LINK_TARGET"):
            assert secret not in encoded
    saved = {path: path.read_bytes() for path in case.artifacts}
    with pytest.raises(FileExistsError):
        case.command(label, accepted=False, verify=verify)
    assert len(children) == 1
    assert all(path.read_bytes() == data for path, data in saved.items())


@pytest.mark.parametrize("failure", ("assertion", "mutation", "timeout", "worker-loss", "launch"))
def test_failed_invocations_persist_actual_final_outcomes(receipt_case, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    case = receipt_case
    label = "failed-" + failure
    real_popen = subprocess.Popen
    children = []

    def launch(_arguments, **kwargs):
        _receipt(case, label, "before")
        if failure == "launch":
            raise OSError("PRIVATE_LAUNCH_ERROR_BODY")
        if failure == "timeout":
            process = real_popen((sys.executable, "-I", "-B", "-c", "import time; time.sleep(120)"), **kwargs)
            communicate = process.communicate
            calls = 0

            def bounded_communicate(*args, **options):
                nonlocal calls
                calls += 1
                options["timeout"] = 0.05 if calls == 1 else options["timeout"]
                return communicate(*args, **options)

            monkeypatch.setattr(process, "communicate", bounded_communicate)
        else:
            prefix = ""
            if failure == "mutation":
                prefix = "from pathlib import Path; Path(" + repr(str(case.oauth)) + ").write_bytes(b'CHANGED_PRIVATE_STATE');\n"
            elif failure == "worker-loss":
                prefix = "import os,signal; os.kill(" + str(case.worker.pid) + ",signal.SIGTERM);\n"
            payload = {"status": "invalid"} if failure == "assertion" else _report(accepted=False)
            process = real_popen((sys.executable, "-I", "-B", "-c", prefix + _REPORT_CHILD, json.dumps(payload), "2"), **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(fixture.subprocess, "Popen", launch)
    with pytest.raises((AssertionError, OSError, subprocess.TimeoutExpired)):
        case.command(label, accepted=False, verify=False)
    after = _receipt(case, label, "after")
    assert after["status"] == "failed"
    assert "PRIVATE_LAUNCH_ERROR_BODY" not in json.dumps(after)
    if failure == "launch":
        assert after["process"]["pid"] is None and after["process"]["returncode"] is None
        assert after["invocation_error"] == "OSError"
    elif failure == "timeout":
        assert children[0].poll() == -signal.SIGKILL
        assert after["process"]["returncode"] == -signal.SIGKILL
        assert after["process"]["termination_signal"] == signal.SIGKILL
        assert after["process"]["cleanup_signal_requested"] == signal.SIGKILL
        assert after["invocation_error"] == "TimeoutExpired"
        assert after["unqualified_before_after_equal"] is True
    elif failure == "mutation":
        assert after["unqualified_before_after_equal"] is False
        assert after["comparisons"][0]["equal"] is False
        assert after["fingerprint"] is not None
    elif failure == "worker-loss":
        assert after["capture_error"] == "AssertionError"
        assert after["fingerprint"] is None
        assert after["unqualified_before_after_equal"] is None
        assert after["comparisons"][0]["equal"] is None
    else:
        assert after["invocation_error"] == "AssertionError"
        assert after["process"]["returncode"] == 2
        assert after["unqualified_before_after_equal"] is True


def test_failed_pre_capture_is_persisted_and_never_launches(receipt_case, monkeypatch: pytest.MonkeyPatch) -> None:
    case = receipt_case
    case.worker.terminate()
    case.worker.wait(timeout=5)
    monkeypatch.setattr(fixture.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("failed before snapshot launched a CLI"))
    with pytest.raises(AssertionError, match="preserved worker exited"):
        case.command("before-unavailable", accepted=False, verify=False)
    before, after = _receipt(case, "before-unavailable", "before"), _receipt(case, "before-unavailable", "after")
    assert before["capture_error"] == after["capture_error"] == "AssertionError"
    assert before["fingerprint"] is after["fingerprint"] is None
    assert after["status"] == "failed" and after["process"]["pid"] is None
    assert after["comparisons"][0]["equal"] is None


def test_receipts_cannot_be_written_inside_protected_paths(receipt_case) -> None:
    case = receipt_case
    case.protected += (case.root,)
    with pytest.raises(AssertionError, match="outside protected paths"):
        fixture.PreservationReceipt(case, "wrong-destination", "observe").capture("before")
    assert list((case.root / "evidence").iterdir()) == []


@pytest.mark.parametrize("kill", (False, True))
def test_actual_pause_race_and_death_receipts_keep_distinct_preservation_intervals(receipt_case, monkeypatch: pytest.MonkeyPatch, kill: bool) -> None:
    case = receipt_case
    original = case.config.read_bytes()
    label = "producer-death" if kill else "configuration-race"
    real_popen = subprocess.Popen
    children = []

    def launch(_arguments, **kwargs):
        if not children:
            _receipt(case, label, "before")
            process = real_popen((sys.executable, "-I", "-B", "-c", _RACE_CHILD, json.dumps(_report(accepted=False)), "2", str(case.config)), **kwargs)
        else:
            # The producer's final record must precede the independent command
            # and, for the race, still contain the actual changed configuration.
            producer = _receipt(case, label, "after")
            assert producer["process"]["returncode"] == (-signal.SIGKILL if kill else 2)
            assert case.config.read_bytes() == (original if kill else original + b"\n")
            process = _spawn_report(real_popen, kwargs, accepted=kill)
        children.append(process)
        return process

    monkeypatch.setattr(fixture.subprocess, "Popen", launch)
    fixture._race_and_producer_death(case, kill=kill)
    assert len(children) == 2 and all(child.poll() is not None for child in children)
    before, paused, after = (_receipt(case, label, phase) for phase in ("before", "paused", "after"))
    assert before["fingerprint"] == paused["fingerprint"]
    assert before["finished_monotonic_ns"] < paused["started_monotonic_ns"] <= paused["finished_monotonic_ns"] < after["started_monotonic_ns"]
    assert after["status"] == "passed"
    assert after["comparisons"][0] == {"from": "before", "to": "paused", "excluded_paths": [], "equal": True}
    assert case.config.read_bytes() == original
    if kill:
        assert after["intentional_mutation"] is None
        assert after["unqualified_before_after_equal"] is True
        assert after["process"]["termination_signal"] == signal.SIGKILL
        assert after["comparisons"][1] == {"from": "before", "to": "after", "excluded_paths": [], "equal": True}
        assert not (case.root / "evidence" / (label + ".json")).exists()
    else:
        changed = _receipt(case, label, "post-fixture-mutation")
        assert changed["fingerprint"] == after["fingerprint"]
        assert after["unqualified_before_after_equal"] is False
        mutation = after["intentional_mutation"]
        assert mutation["path"] == str(case.config) and mutation["write_completed"] is True
        assert mutation["preservation_claimed"] is False
        assert {row["sha256"] for row in mutation["before"]} == {hashlib.sha256(original).hexdigest()}
        assert {row["sha256"] for row in mutation["after"]} == {hashlib.sha256(original + b"\n").hexdigest()}
        assert after["comparisons"][1:] == [
            {"from": "before", "to": "after", "excluded_paths": [str(case.config)], "equal": True},
            {"from": "post-fixture-mutation", "to": "after", "excluded_paths": [], "equal": True},
        ]
