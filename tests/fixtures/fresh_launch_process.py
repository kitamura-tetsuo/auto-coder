"""Synthetic actual CLI/child fixture; container paths and host transport only.

The installed/console runtime's physical Popen argv is replaced by a wait-gate
child. That child never opens ancestor fences. No Docker or real state is used.
"""

import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import bootstrap_supervisor, fresh_initialization, retained_startup
from auto_coder.management.bootstrap_supervisor import ResultStatus
from tests.test_management_fresh_launch_exclusions import first_launch_case

CHILD = """import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); raw=Path('/proc/self/stat').read_text()
(root/'child.json').write_text(json.dumps({'pid':os.getpid(),'started':raw[raw.rfind(')')+2:].split()[19]}))
with (root/'launches').open('a') as stream: stream.write('launch\\n')
if sys.argv[2]=='early-exit': raise SystemExit(0)
(root/'child-ready').touch()
while not (root/'child-release').exists(): time.sleep(.01)
"""

HOLDER = """import fcntl,json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); streams=[Path(p).open('r') for p in sys.argv[2:]]
for stream in streams: fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
raw=Path('/proc/self/stat').read_text()
(root/'holder.json').write_text(json.dumps({'pid':os.getpid(),'started':raw[raw.rfind(')')+2:].split()[19]}))
(root/'holder-ready').touch()
while not (root/'holder-release').exists(): time.sleep(.01)
"""


def main():
    root, requested_fault = Path(sys.argv[1]), sys.argv[2]
    original_profile = requested_fault.startswith("original-")
    fault = requested_fault.removeprefix("original-") if original_profile else requested_fault
    patches = pytest.MonkeyPatch()
    profile = "original-history" if original_profile else "clean-restart" if fault == "prelaunch-shutdown" else None
    fixture = first_launch_case.__wrapped__(root, patches, SimpleNamespace(param=profile))
    case = next(fixture)
    case.owner.close()  # main()/run() must acquire the real exclusive owner FD.
    if original_profile:
        # The fixture's writable placeholder is not an original owner claim.
        # Require the actual run() producer to create the 0600 owner inode.
        (case.state / ".supervisor-owner.lock").unlink()
    real_popen = subprocess.Popen
    holder = real_popen([sys.executable, "-I", "-c", HOLDER, str(root), str(case.a / "owner.lock"), str(case.b / "owner.lock")], start_new_session=True)
    deadline = time.monotonic() + 10
    while not (root / "holder-ready").exists():
        assert holder.poll() is None and time.monotonic() < deadline
        time.sleep(0.01)
    grant = fresh_initialization.request_fresh_host_grant

    def grant_and_release(*args, **kwargs):
        response = grant(*args, **kwargs)
        (root / "holder-release").touch()
        holder.wait(timeout=5)
        (root / "host-exited").touch()
        if fault == "prelaunch-shutdown":
            os.kill(os.getpid(), signal.SIGTERM)
        return response

    patches.setattr(fresh_initialization, "request_fresh_host_grant", grant_and_release)

    physical_pids = []

    def physical_popen(argv, **kwargs):
        assert argv[0] == str(case.paths[2])
        child = real_popen([sys.executable, "-I", "-c", CHILD, str(root), fault], **kwargs)
        physical_pids.append(child.pid)
        if fault == "lost-handle":
            # Model an adapter losing acknowledgement after exec, without
            # returning the Popen handle to the owning supervisor.
            deadline = time.monotonic() + 10
            while not (root / "child-ready").exists():
                assert child.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
            raise OSError("synthetic adapter lost the created process handle")
        return child

    # Replace only this module's subprocess reference; other subprocess users
    # (including the independent host holder) keep the actual standard module.
    patches.setattr(bootstrap_supervisor, "subprocess", SimpleNamespace(Popen=physical_popen, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError))
    if fault == "unobservable-start":
        start_token = bootstrap_supervisor.ManagementBootstrapSupervisor._start_token

        def missing_child_token(pid):
            assert os.getpid() not in physical_pids
            return "" if pid in physical_pids else start_token(pid)

        patches.setattr(bootstrap_supervisor.ManagementBootstrapSupervisor, "_start_token", staticmethod(missing_child_token))
    original_start = bootstrap_supervisor.ManagementBootstrapSupervisor.start_selected

    def start_then_fault(self, *args, **kwargs):
        observed = original_start(self, *args, **kwargs)
        (root / "start-returned").touch()
        if fault == "unknown-start":
            observed = replace(observed, status=ResultStatus.UNKNOWN)
        if fault == "start-exception":
            raise RuntimeError("disposable post-Popen acknowledgement failure")
        return observed

    patches.setattr(bootstrap_supervisor.ManagementBootstrapSupervisor, "start_selected", start_then_fault)
    if fault == "record-error":

        def lose_record(*args, **kwargs):
            (root / "record-failed").touch()
            raise OSError("disposable launch record failure")

        patches.setattr(fresh_initialization, "record_fresh_launch", lose_record)
    if fault == "prelaunch-refusal":
        patches.setattr(retained_startup, "retained_from_fresh_intent", lambda *args, **kwargs: None)
    sys.argv = ["bootstrap-supervisor", "--config", str(case.supervisor.bootstrap_config_path), "--fresh-initialization"]
    try:
        bootstrap_supervisor.main()
    finally:
        fixture.close()
        patches.undo()


if __name__ == "__main__":
    main()
