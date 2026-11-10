"""Real OS component tests; no deployment/profile acceptance is asserted."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from auto_coder.management import proc_observation as proc

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
ROOT = Path(__file__).resolve().parents[1]


def _wait_sleeping(child: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        assert child.poll() is None
        tasks = Path(f"/proc/{child.pid}/task")
        states = [(path / "stat").read_text().rpartition(")")[2].split()[0] for path in tasks.iterdir()]
        if states and set(states) == {"S"}:
            return
        time.sleep(0.005)
    pytest.fail("real fixture tasks did not enter observable sleeping state")


@contextmanager
def _child(code: str):
    child = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        ready = child.stdout.readline().strip()
        assert ready == "ready", (ready, child.poll(), child.stderr.read() if child.poll() is not None else "")
        _wait_sleeping(child)
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def _command(tmp_path: Path, pid: int, previous: dict | None = None) -> dict:
    command = [sys.executable, "-m", "auto_coder.management.proc_observation", "observe" if previous is None else "verify", "--pid", str(pid)]
    if previous is not None:
        saved = tmp_path / "previous.json"
        saved.write_text(json.dumps(previous))
        command += ["--previous", str(saved)]
    result = subprocess.run(command, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stderr
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert value["status"] == "unavailable"
    return value


def test_real_nonleader_task_lifetimes_namespaces_and_cgroups(tmp_path: Path) -> None:
    with _child("import threading, sys\nevent = threading.Event()\nt = threading.Thread(target=event.wait, daemon=True)\nt.start()\nprint('ready', flush=True)\nsys.stdin.read()") as child:
        before_fds = set(os.listdir("/proc/self/fd"))
        observed = proc.observe_process(child.pid)
        assert observed.unavailable == ()
        assert observed.boot_id == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        assert len(observed.tasks) == 2
        assert {task.tid for task in observed.tasks} == {int(name) for name in os.listdir(f"/proc/{child.pid}/task")}
        assert observed.start_token == next(task.start_token for task in observed.tasks if task.tid == child.pid)
        for task in observed.tasks:
            base = Path(f"/proc/{child.pid}/task/{task.tid}")
            assert task.state == "S"
            assert task.start_token == (base / "stat").read_text().rpartition(")")[2].split()[19]
            assert dict(task.namespaces)["net"] == os.readlink(base / "ns/net")
            assert dict(task.namespaces)["pid"] == os.readlink(base / "ns/pid")
            assert task.cgroups == tuple(sorted((base / "cgroup").read_text().splitlines()))
        assert set(os.listdir("/proc/self/fd")) == before_fds
        saved = _command(tmp_path, child.pid)
        assert saved["processes"][0]["unavailable"] == []
        assert not Path(f'/proc/{saved["observer_pid"]}').exists()
        current = _command(tmp_path, child.pid, saved)
        assert current["observer_pid"] != saved["observer_pid"]
        assert current["observation_id"] != saved["observation_id"]
        assert current["processes"] == saved["processes"]
        assert len(current["unavailable"]) == 1


def test_consumer_reobserves_exited_target_and_refuses_forged_lifetime(tmp_path: Path) -> None:
    with _child("import sys\nprint('ready', flush=True)\nsys.stdin.read()") as child:
        saved = _command(tmp_path, child.pid)
        forged = json.loads(json.dumps(saved))
        forged["processes"][0]["start_token"] = "1"
        current = _command(tmp_path, child.pid, forged)
        assert "Independent current observation differs: processes" in current["unavailable"]
        assert current["processes"][0]["start_token"] == saved["processes"][0]["start_token"]
    current = _command(tmp_path, child.pid, saved)
    assert current["processes"][0]["unavailable"]
    assert current["processes"][0]["tasks"] == []
    assert "Independent current observation differs: processes" in current["unavailable"]


def test_zombie_is_preserved_as_zombie_not_absence() -> None:
    # waitid(WNOWAIT) synchronizes exit without reaping the real zombie.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        observed = proc.observe_process(child.pid)
        assert observed.start_token.isdecimal()
        assert observed.tasks == (proc.TaskFact(child.pid, observed.start_token, "Z"),)
        assert observed.unavailable == ("ValueError: zombie/dead task observed; not absence",)
        assert observed.listeners == ()
    finally:
        child.wait(timeout=10)


def test_real_unix_listener_inode_is_observed_and_consumer_detects_release(tmp_path: Path) -> None:
    # A sandbox EPERM here must fail visibly for authorized host execution.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(tmp_path / "listener.sock"))
        listener.listen(1)
        inode = int(os.readlink(f"/proc/self/fd/{listener.fileno()}")[8:-1])
        # Transfer ownership to a blocked child so observing does not scan its
        # own transient proc descriptors. No mock listener or success oracle.
        code = "import os, sys; print('ready', flush=True); sys.stdin.readline(); os.close(int(sys.argv[1])); print('released', flush=True); sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-u", "-c", code, str(listener.fileno())], pass_fds=(listener.fileno(),), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ready"
            _wait_sleeping(child)
            listener.close()
            saved = _command(tmp_path, child.pid)
            facts = saved["processes"][0]
            assert facts["unavailable"] == []
            assert {"transport": "unix", "inode": inode, "local_address": "", "state": "01"} in facts["listeners"]
            assert inode in facts["tasks"][0]["socket_inodes"]
            child.stdin.write("release-listener\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "released"
            _wait_sleeping(child)
            current = _command(tmp_path, child.pid, saved)
            assert child.poll() is None
            assert "Independent current observation differs: processes" in current["unavailable"]
            facts = current["processes"][0]
            assert facts["unavailable"] == []
            assert inode not in facts["tasks"][0]["socket_inodes"]
            assert inode not in {item["inode"] for item in facts["listeners"]}
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
    finally:
        listener.close()


@pytest.mark.parametrize("pid", [0, -1, True, 2147483648])
def test_invalid_pid_is_unavailable_not_absent(pid: int) -> None:
    result = proc.observe_process(pid)
    assert result.tasks == ()
    assert result.unavailable == ("ValueError: positive bounded process ID required",)


def test_proc_fixture_access_failure_and_observable_race_are_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit boundary failures supplement real OS tests, not profile acceptance."""
    with _child("import sys\nprint('ready', flush=True)\nsys.stdin.read()") as child:
        original = proc._task

        def denied(path: Path, tid: int) -> proc.TaskFact:
            raise PermissionError(13, "fixture task inaccessible")

        before_fds = set(os.listdir("/proc/self/fd"))
        monkeypatch.setattr(proc, "_task", denied)
        denied_result = proc.observe_process(child.pid)
        assert set(os.listdir("/proc/self/fd")) == before_fds
        assert denied_result.tasks[0].start_token.isdecimal()
        assert denied_result.unavailable == ("PermissionError: [Errno 13] fixture task inaccessible",)
        calls = 0

        def changed(path: Path, tid: int) -> proc.TaskFact:
            nonlocal calls
            calls += 1
            value = original(path, tid)
            return value if calls == 1 else proc.replace(value, start_token="1")

        monkeypatch.setattr(proc, "_task", changed)
        result = proc.observe_process(child.pid)
        assert calls == 2
        assert result.unavailable == ("ValueError: process/task/namespace/socket observation changed",)


def test_old_boot_namespace_and_authorizing_replay_are_not_authority(tmp_path: Path) -> None:
    with _child("import sys\nprint('ready', flush=True)\nsys.stdin.read()") as child:
        saved = _command(tmp_path, child.pid)
        for field, value in (("boot_id", "00000000-0000-0000-0000-000000000000"), ("start_token", "1")):
            forged = json.loads(json.dumps(saved))
            forged["processes"][0][field] = value
            result = _command(tmp_path, child.pid, forged)
            assert "Independent current observation differs: processes" in result["unavailable"]
        saved["status"] = "contained"
        result = _command(tmp_path, child.pid, saved)
        assert "Unsupported or authorizing saved process report" in result["unavailable"]


def test_real_thread_creation_between_reads_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code = "import sys, threading\nprint('ready', flush=True)\nsys.stdin.readline()\nthreading.Thread(target=threading.Event().wait, daemon=True).start()\nprint('thread-created', flush=True)\nsys.stdin.read()"
    with _child(code) as child:
        original = proc._task
        calls = 0

        def interleaved(path: Path, tid: int) -> proc.TaskFact:
            nonlocal calls
            value = original(path, tid)
            calls += 1
            if calls == 1:
                child.stdin.write("create-thread\n")
                child.stdin.flush()
                assert child.stdout.readline().strip() == "thread-created"
                _wait_sleeping(child)
            return value

        monkeypatch.setattr(proc, "_task", interleaved)
        observed = proc.observe_process(child.pid)
        assert calls == 2
        assert len(os.listdir(f"/proc/{child.pid}/task")) == 2
        assert observed.unavailable == ("ValueError: process/task/namespace/socket observation changed",)


def test_os_socket_table_fixture_missing_malformed_and_bounded(tmp_path: Path) -> None:
    # Copy actual OS tables; removing/corrupting one must not become an empty scan.
    net = tmp_path / "net"
    net.mkdir()
    for name in ("tcp", "tcp6", "udp", "udp6", "unix"):
        (net / name).write_bytes((Path("/proc/self/net") / name).read_bytes())
    proc._listeners(tmp_path)
    (net / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\nbroken\n")
    with pytest.raises(ValueError, match="invalid Unix socket row"):
        proc._listeners(tmp_path)
    (net / "unix").unlink()
    with pytest.raises(FileNotFoundError):
        proc._listeners(tmp_path)
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"a" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds size limit"):
        proc._read(oversized)


def test_historical_namespace_string_cannot_override_current_task(tmp_path: Path) -> None:
    with _child("import sys\nprint('ready', flush=True)\nsys.stdin.read()") as child:
        saved = _command(tmp_path, child.pid)
        saved["processes"][0]["tasks"][0]["namespaces"] = [["net", "net:[1]"]]
        current = _command(tmp_path, child.pid, saved)
        assert "Independent current observation differs: processes" in current["unavailable"]
        assert dict(current["processes"][0]["tasks"][0]["namespaces"])["net"] == os.readlink(f"/proc/{child.pid}/ns/net")
