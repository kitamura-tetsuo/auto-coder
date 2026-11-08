"""Actual CLI/process ownership regression, with explicit component controls.

The fixture substitutes container path/RO mount and grant transport boundaries,
and only physical runtime argv. CLI main/run/start_selected, Popen, SQLite,
kernel locks, independent host holder and EX contenders are real. This is not
Docker, installed READY or host mount-namespace conformance evidence.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

CONTENDER = """import fcntl,json,sys
from pathlib import Path
result=[]
for path in sys.argv[1:]:
 with Path(path).open('r') as stream:
  try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError: result.append(False)
  else: result.append(True)
print(json.dumps(result))
"""


def wait_for(predicate, description, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError(description)


def available_fences(root):
    paths = [root / "a/owner.lock", root / "b/owner.lock", root / "state/.supervisor-owner.lock"]
    output = subprocess.check_output([sys.executable, "-I", "-c", CONTENDER, *map(str, paths)], text=True, timeout=5)
    return json.loads(output)


def alive(record):
    try:
        raw = Path(f"/proc/{record['pid']}/stat").read_text()
    except FileNotFoundError:
        return False
    fields = raw[raw.rfind(")") + 2 :].split()
    assert fields[19] == record["started"], "disposable process identity changed"
    return fields[0] != "Z"


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
@pytest.mark.parametrize(
    "fault,interrupt",
    [(fault, None) for fault in ("record-error", "unknown-start", "start-exception", "early-exit", "prelaunch-refusal", "prelaunch-shutdown", "unobservable-start", "lost-handle")]
    + [(fault, interrupt) for fault in ("record-error", "unknown-start", "start-exception") for interrupt in (signal.SIGTERM, signal.SIGINT)],
)
def test_cli_retains_all_fences_until_possible_fresh_child_exits(tmp_path, fault, interrupt):
    repository = Path(__file__).resolve().parents[1]
    log = (tmp_path / "driver.log").open("w")
    process = subprocess.Popen([sys.executable, "-m", "tests.fixtures.fresh_launch_process", str(tmp_path), fault], cwd=repository, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait_for(lambda: (tmp_path / "host-exited").exists() or process.poll() is not None, "actual host grant did not complete")
        assert (tmp_path / "host-exited").exists(), (tmp_path / "driver.log").read_text()
        holder = json.loads((tmp_path / "holder.json").read_text())
        assert not alive(holder), "host's extra SH locks must already be gone"
        # Publication creates the claim file before writing its bytes. Observe
        # the downstream dispatch boundary or CLI exit before snapshotting it.
        wait_for(lambda: (tmp_path / "start-returned").exists() or process.poll() is not None, "first-launch claim did not reach dispatch or a terminal refusal")
        claim_path = tmp_path / "state/fresh-launch-claimed.json"
        wait_for(claim_path.is_file, "durable first-launch claim was not published")
        claim = claim_path.read_bytes()
        if fault in {"early-exit", "prelaunch-refusal", "prelaunch-shutdown"}:
            process.wait(timeout=10)
            assert process.returncode != 0
            assert available_fences(tmp_path) == [True, True, True]
            expected = 1 if fault == "early-exit" else 0
            if expected:
                assert len((tmp_path / "launches").read_text().splitlines()) == expected
            else:
                assert not (tmp_path / "launches").exists()
        else:
            wait_for(lambda: (tmp_path / "start-returned").exists(), "real start_selected did not return")
            if fault == "record-error":
                wait_for(lambda: (tmp_path / "record-failed").exists(), "launch recording never reached its fault boundary")
            wait_for(lambda: (tmp_path / "child-ready").exists(), "physical child never reached its gate")
            child = json.loads((tmp_path / "child.json").read_text())
            assert alive(child) and (tmp_path / "child-ready").exists()
            # A brief observation after the exact fault boundary detects the
            # baseline CLI exit without using startup wall time as correctness.
            time.sleep(0.25)
            observed = {"cli_exit": process.poll(), "child_alive": alive(child), "fences_available": available_fences(tmp_path)}
            assert observed == {"cli_exit": None, "child_alive": True, "fences_available": [False, False, False]}, observed
            if interrupt is not None:
                os.kill(process.pid, interrupt)
                time.sleep(0.25)
                assert process.poll() is None and alive(child), "termination request released an unresolved child"
                assert available_fences(tmp_path) == [False, False, False]
            assert (tmp_path / "state/fresh-launch-claimed.json").read_bytes() == claim
            assert (tmp_path / "launches").read_text().splitlines() == ["launch"]
            (tmp_path / "child-release").touch()
            if fault == "lost-handle":
                wait_for(lambda: not alive(child), "untracked child did not naturally exit")
                time.sleep(0.25)
                assert process.poll() is None, "unavailable handle was treated as positive absence"
                assert available_fences(tmp_path) == [False, False, False]
            else:
                process.wait(timeout=10)
                assert process.returncode != 0
                assert not alive(child)
                assert available_fences(tmp_path) == [True, True, True]
        assert (tmp_path / "state/fresh-launch-claimed.json").read_bytes() == claim
    finally:
        (tmp_path / "holder-release").touch()
        (tmp_path / "child-release").touch()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        # The normal child shares only this disposable driver's process group.
        for name in ("child.json", "holder.json"):
            path = tmp_path / name
            if path.exists():
                record = json.loads(path.read_text())
                if alive(record):
                    if name == "holder.json":
                        os.killpg(record["pid"], signal.SIGKILL)
                    else:
                        os.kill(record["pid"], signal.SIGKILL)
                    wait_for(lambda: not alive(record), "disposable process leaked", timeout=5)
        log.close()
