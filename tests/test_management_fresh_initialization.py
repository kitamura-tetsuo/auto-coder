"""Durable fresh-instance intent does not itself authorize a launch."""

import errno
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml

from auto_coder.management.fresh_host_observation import HostObservationUnavailable, assert_host_listener_absent, assert_peer_holds_shared_fence, assert_prior_network_pin, assert_prior_network_unreferenced
from auto_coder.management.fresh_initialization import (
    FreshInitializationIntent,
    acquire_prior_owner_fence,
    application_tree_sha256,
    claim_fresh_initialization,
    read_fresh_status,
    record_fresh_launch,
    record_fresh_outcome,
    request_and_claim_first_launch,
    request_fresh_host_grant,
    verify_initialization_inputs,
)


def _intent(root: Path) -> FreshInitializationIntent:
    (root / "state").mkdir(mode=0o700, exist_ok=True)
    (root / "journal").mkdir(mode=0o700)
    (root / "oauth").mkdir(mode=0o700)
    (root / "guard").mkdir(mode=0o700)
    state, journal, oauth, guard = ((root / name).stat() for name in ("state", "journal", "oauth", "guard"))
    return FreshInitializationIntent(
        str(uuid.uuid4()),
        "old-instance",
        "a" * 64,
        "new-instance",
        "9" * 64,
        "8" * 64,
        1,
        2,
        8080,
        "b" * 64,
        "c" * 64,
        "sha256:" + "d" * 64,
        "e" * 64,
        "f" * 64,
        "bootstrap",
        "/container/state",
        "/container/journal",
        "/container/oauth",
        str(root / "state"),
        str(root / "journal"),
        str(root / "oauth"),
        state.st_dev,
        state.st_ino,
        journal.st_dev,
        journal.st_ino,
        oauth.st_dev,
        oauth.st_ino,
        guard.st_dev,
        guard.st_ino,
    )


def test_fresh_claim_retains_provenance_and_never_reuses_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    intent = _intent(tmp_path)
    oauth = tmp_path / "oauth" / "retained-evidence"
    oauth.write_bytes(b"unchanged")
    digest = claim_fresh_initialization(state, intent)
    assert len(digest) == 64
    record = state / "fresh-initialization.json"
    original = record.read_bytes()
    assert intent.request_id.encode() in original
    assert record.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        claim_fresh_initialization(state, intent)
    with pytest.raises(FileExistsError):
        claim_fresh_initialization(state, replace(intent, request_id=str(uuid.uuid4())))
    assert record.read_bytes() == original
    assert oauth.read_bytes() == b"unchanged"
    assert list((tmp_path / "journal").iterdir()) == []


def test_fresh_claim_rejects_conflicts_and_unprotected_roots(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    intent = _intent(tmp_path)
    for invalid in (replace(intent, new_instance=intent.old_instance), replace(intent, executable_sha256="wrong"), replace(intent, journal_root=intent.state_root)):
        with pytest.raises(ValueError):
            claim_fresh_initialization(state, invalid)
    state.chmod(0o775)
    with pytest.raises(PermissionError):
        claim_fresh_initialization(state, intent)
    state.chmod(0o700)
    (tmp_path / "journal" / "partial").touch()
    with pytest.raises(FileExistsError):
        claim_fresh_initialization(state, intent)
    (tmp_path / "journal" / "partial").unlink()
    (state / "partial").touch()
    with pytest.raises(FileExistsError):
        claim_fresh_initialization(state, intent)


def test_concurrent_fresh_claims_have_one_winner(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    intent = _intent(tmp_path)

    def attempt() -> bool:
        try:
            claim_fresh_initialization(state, intent)
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]


def test_fresh_claim_contends_across_actual_processes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    intent = _intent(tmp_path)
    intent_path = tmp_path / "intent.json"
    intent_path.write_text(json.dumps(asdict(intent)))
    code = """import json,sys
from pathlib import Path
from auto_coder.management.fresh_initialization import FreshInitializationIntent,claim_fresh_initialization
i=FreshInitializationIntent(**json.loads(Path(sys.argv[1]).read_text()))
print('ready',flush=True); sys.stdin.read(1)
try:
 claim_fresh_initialization(Path(i.host_state_root),i)
 print('won',flush=True)
except FileExistsError:
 print('refused',flush=True)
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(intent_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            assert process.stdout is not None and process.stdout.readline().strip() == "ready"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("x")
            process.stdin.flush()
        outcomes = []
        for process in processes:
            assert process.stdout is not None
            outcomes.append(process.stdout.readline().strip())
            assert process.wait(timeout=10) == 0
        assert sorted(outcomes) == ["refused", "won"]
        assert (state / "fresh-initialization.json").is_file()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_kernel_fence_handoff_blocks_old_owner_until_successor_releases(tmp_path: Path) -> None:
    lock = tmp_path / "prior-owner.lock"
    lock.touch()
    info = lock.stat()
    old_owner = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(old_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            acquire_prior_owner_fence(lock, expected_device=info.st_dev, expected_inode=info.st_ino)
        fcntl.flock(old_owner, fcntl.LOCK_UN)
        guard = acquire_prior_owner_fence(lock, expected_device=info.st_dev, expected_inode=info.st_ino)
        successor = acquire_prior_owner_fence(lock, expected_device=info.st_dev, expected_inode=info.st_ino)
        try:
            os.close(guard)
            with pytest.raises(BlockingIOError):
                fcntl.flock(old_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(successor)
        fcntl.flock(old_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError):
            acquire_prior_owner_fence(lock, expected_device=info.st_dev, expected_inode=info.st_ino + 1)
    finally:
        os.close(old_owner)


def test_kernel_lock_table_identifies_actual_successor_process(tmp_path: Path) -> None:
    lock = tmp_path / "prior-owner.lock"
    lock.touch()
    holder = subprocess.Popen([sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_SH); print('ready',flush=True); sys.stdin.read(1)", str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    descriptor = os.open(lock, os.O_RDONLY)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        assert_peer_holds_shared_fence(holder.pid, descriptor)
        with pytest.raises(HostObservationUnavailable):
            assert_peer_holds_shared_fence(os.getpid(), descriptor)
    finally:
        assert holder.stdin is not None
        holder.stdin.write("x")
        holder.stdin.flush()
        holder.wait(timeout=5)
        os.close(descriptor)


def test_initialization_binds_actual_config_executable_and_application_closure(tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    bootstrap = tmp_path / "bootstrap.json"
    management = tmp_path / "management.yaml"
    executable = tmp_path / "executable"
    application = tmp_path / "application"
    application.mkdir()
    module = application / "module.py"
    for path, content in ((bootstrap, b"bootstrap"), (management, b"management"), (executable, b"launcher"), (module, b"application")):
        path.write_bytes(content)
    intent = replace(
        intent, bootstrap_config_sha256=hashlib.sha256(bootstrap.read_bytes()).hexdigest(), management_config_sha256=hashlib.sha256(management.read_bytes()).hexdigest(), executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(), application_sha256=application_tree_sha256(application)
    )
    verify_initialization_inputs(intent, bootstrap_config=bootstrap, management_config=management, executable=executable, application_root=application)
    module.write_bytes(b"different application")
    with pytest.raises(ValueError):
        verify_initialization_inputs(intent, bootstrap_config=bootstrap, management_config=management, executable=executable, application_root=application)
    module.unlink()
    module.symlink_to(executable)
    with pytest.raises(ValueError):
        application_tree_sha256(application)


def test_namespace_pin_rejects_missing_or_different_identity() -> None:
    descriptor = os.open("/proc/self/ns/net", os.O_RDONLY)
    try:
        namespace = os.readlink("/proc/self/ns/net")
        assert_prior_network_pin(descriptor, namespace)
        with pytest.raises(HostObservationUnavailable):
            assert_prior_network_pin(descriptor, "net:[1]")
    finally:
        os.close(descriptor)
    with pytest.raises(HostObservationUnavailable):
        assert_prior_network_pin(descriptor, namespace)


def test_namespace_absence_checks_every_task_and_rejects_incomplete_evidence(tmp_path: Path) -> None:
    from auto_coder.management.fresh_host_observation import _assert_all_task_networks_absent

    leader = tmp_path / "42" / "task" / "42" / "ns"
    worker = tmp_path / "42" / "task" / "43" / "ns"
    leader.mkdir(parents=True)
    worker.mkdir(parents=True)
    (leader / "net").symlink_to("net:[101]")
    (worker / "net").symlink_to("net:[202]")
    with pytest.raises(HostObservationUnavailable):
        _assert_all_task_networks_absent(tmp_path, "net:[202]")
    _assert_all_task_networks_absent(tmp_path, "net:[303]")
    (worker / "net").unlink()
    with pytest.raises(ValueError):
        _assert_all_task_networks_absent(tmp_path, "net:[303]")


def test_host_proc_profile_rejects_hidden_or_nested_views() -> None:
    from auto_coder.management.fresh_host_observation import _validate_host_proc_profile

    mount = "53 44 0:25 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw"
    uid_map = "0 0 4294967295"
    _validate_host_proc_profile("pid:[4026531836]", uid_map, mount)
    for pidns, mapping, mounts in (
        ("pid:[1234]", uid_map, mount),
        ("pid:[4026531836]", "0 1000 1", mount),
        ("pid:[4026531836]", uid_map, mount + ",hidepid=2"),
        ("pid:[4026531836]", uid_map, mount + "\n54 53 0:26 / /proc/42 rw - tmpfs tmpfs rw"),
    ):
        with pytest.raises(ValueError):
            _validate_host_proc_profile(pidns, mapping, mounts)


def test_actual_host_listener_and_live_namespace_refuse() -> None:
    namespace = os.readlink("/proc/self/ns/net")
    with pytest.raises(HostObservationUnavailable):
        assert_prior_network_unreferenced(namespace)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(HostObservationUnavailable):
            assert_host_listener_absent(port)


@pytest.mark.parametrize("tamper", [False, True])
def test_real_unix_grant_is_bound_to_current_challenge(tmp_path: Path, tamper: bool) -> None:
    path = tmp_path / "guard.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen()

        def serve() -> None:
            connection, _ = listener.accept()
            with connection:
                with connection.makefile("rb") as stream:
                    request = json.loads(stream.readline(4097))
                if tamper:
                    request["challenge"] = "0" * 64
                connection.sendall(json.dumps({**request, "granted": "host-evidence-verified"}).encode() + b"\n")

        worker = threading.Thread(target=serve)
        worker.start()
        arguments = dict(request_id=str(uuid.uuid4()), intent_sha256="a" * 64, owner_incarnation=str(uuid.uuid4()), new_container_id="b" * 64, trusted_uid=os.geteuid())
        try:
            if tamper:
                with pytest.raises(ValueError):
                    request_fresh_host_grant(path, **arguments)
            else:
                assert len(request_fresh_host_grant(path, **arguments)["challenge"]) == 64
        finally:
            worker.join(timeout=5)
        assert not worker.is_alive()


def test_first_launch_claim_is_durable_and_never_repeated(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    intent = replace(_intent(tmp_path), state_root=str(state))
    claim_fresh_initialization(state, intent)
    assert read_fresh_status(state)["status"] == "pending"
    path = tmp_path / "guard.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen()

        def serve() -> None:
            connection, _ = listener.accept()
            with connection:
                with connection.makefile("rb") as stream:
                    request = json.loads(stream.readline(4097))
                connection.sendall(json.dumps({**request, "granted": "host-evidence-verified"}).encode() + b"\n")

        worker = threading.Thread(target=serve)
        worker.start()
        owner = str(uuid.uuid4())
        try:
            assert request_and_claim_first_launch(state, path, owner_incarnation=owner, trusted_uid=os.geteuid()) == intent
            claim = (state / "fresh-launch-claimed.json").read_bytes()
            assert read_fresh_status(state)["status"] == "unknown"
            with pytest.raises(FileExistsError):
                request_and_claim_first_launch(state, path, owner_incarnation=owner, trusted_uid=os.geteuid())
            assert (state / "fresh-launch-claimed.json").read_bytes() == claim
            observation = record_fresh_launch(state, intent, incarnation=owner, child_identity="123:456:1:bootstrap:owner", verification_timeout=30)
            assert read_fresh_status(state)["status"] == "launched"
            record_fresh_outcome(state, observation, status="succeeded")
            assert read_fresh_status(state)["status"] == "succeeded"
            with pytest.raises(FileExistsError):
                record_fresh_outcome(state, observation, status="unknown")
        finally:
            worker.join(timeout=5)
        assert not worker.is_alive()


def test_real_supervisor_refuses_unapproved_guard_paths(tmp_path: Path) -> None:
    import auto_coder
    from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor

    intent = _intent(tmp_path)
    state, journal, oauth = (tmp_path / name for name in ("state", "journal", "oauth"))
    old_lock = tmp_path / "old-owner.lock"
    old_lock.touch()
    executable = tmp_path / "executable"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    config = tmp_path / "bootstrap.json"
    management = tmp_path / "management.yaml"
    management.write_text(
        yaml.safe_dump(
            {
                "state_root": str(oauth),
                "operation_state_root": str(journal),
                "bootstrap_config_path": str(config),
                "public_origin": "http://127.0.0.1:8081",
                "firebase_project_id": "fixture",
                "clients": [{"client_id": "fixture-client", "redirect_uris": ["http://127.0.0.1:8081/callback"]}],
                "bind_host": "127.0.0.1",
                "port": 8081,
                "local_development": True,
            }
        )
    )
    executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    config.write_text(
        json.dumps(
            {
                "instance_id": "new-instance",
                "state_root": str(state),
                "oauth_root": str(oauth),
                "journal_root": str(journal),
                "listener_url": "http://127.0.0.1:8081/mcp",
                "initial_runtime": "bootstrap",
                "runtimes": [{"reference": "bootstrap", "receipt": "sha256:" + executable_digest, "executable": str(executable), "arguments": ["management", "serve", "--config", str(management)]}],
                "fresh_guard_path": str(tmp_path / "missing-guard.sock"),
                "fresh_prior_lock_path": str(old_lock),
            }
        )
    )
    lock_info = old_lock.stat()
    intent = replace(
        intent,
        state_root=str(state),
        journal_root=str(journal),
        oauth_root=str(oauth),
        owner_lock_device=lock_info.st_dev,
        owner_lock_inode=lock_info.st_ino,
        bootstrap_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        management_config_sha256=hashlib.sha256(management.read_bytes()).hexdigest(),
        executable_sha256=executable_digest,
        application_sha256=application_tree_sha256(Path(auto_coder.__file__).resolve().parent),
    )
    retained = oauth / "retained"
    retained.write_bytes(b"unchanged")
    claim_fresh_initialization(state, intent)
    # Reconstructing without the explicit fresh flag must not turn pending
    # initialization into the ordinary pristine auto-start path.
    reconstructed = subprocess.run(
        [sys.executable, "-m", "auto_coder.management.bootstrap_supervisor", "--config", str(config)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert reconstructed.returncode != 0
    assert not (state / "management-child.sqlite3").exists()
    supervisor = ManagementBootstrapSupervisor(BootstrapConfig.read(config), bootstrap_config_path=config, fresh_initialization=True)
    try:
        assert not supervisor._start_fresh_initialization()
        assert not supervisor.store.path.exists()
        assert not (state / "fresh-launch-claimed.json").exists()
        assert read_fresh_status(state)["status"] == "pending"
        assert not supervisor._start_fresh_initialization()
        assert not (state / "fresh-launched.json").exists()
        assert not list(journal.iterdir())
        assert retained.read_bytes() == b"unchanged"
        assert not supervisor._children
    finally:
        if supervisor._prior_owner_fence is not None:
            os.close(supervisor._prior_owner_fence)


def test_fresh_console_wrapper_rejects_extra_code_and_python_hooks(tmp_path: Path) -> None:
    from auto_coder.management.bootstrap_supervisor import RuntimeSpec, _verify_fresh_console_entrypoint

    wrapper = tmp_path / "auto-coder"
    wrapper.write_text(
        """#!/opt/auto-coder/.venv/bin/python
import sys
from auto_coder.cli import main
if __name__ == "__main__":
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
"""
    )
    runtime = RuntimeSpec("initial", "unused", wrapper, ())
    _verify_fresh_console_entrypoint(runtime)
    with pytest.raises(ValueError):
        _verify_fresh_console_entrypoint(replace(runtime, environment=(("PYTHONPATH", "/tmp"),)))
    marker = tmp_path / "executed"
    wrapper.write_text(wrapper.read_text() + f'\nopen({str(marker)!r}, "w").close()\n')
    with pytest.raises(ValueError):
        _verify_fresh_console_entrypoint(runtime)
    assert not marker.exists()


def test_retained_oauth_preflight_never_creates_or_migrates(tmp_path: Path) -> None:
    from auto_coder.management.fresh_oauth import validate_retained_oauth
    from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
    from auto_coder.management.oauth_store import OAuthStore

    path = tmp_path / "oauth.sqlite3"
    settings = OAuthSettings(path, "http://127.0.0.1:8081", "fixture", (OAuthClient("client", ("http://127.0.0.1:8081/callback",)),))
    with pytest.raises(ValueError):
        validate_retained_oauth(settings)
    assert not path.exists()
    OAuthStore(settings)
    original = path.read_bytes()
    validate_retained_oauth(settings)
    assert path.read_bytes() == original
    assert not Path(str(path) + "-wal").exists()
    wal = Path(str(path) + "-wal")
    wal.write_bytes(b"unavailable-journal")
    with pytest.raises(ValueError):
        validate_retained_oauth(settings)
    assert wal.read_bytes() == b"unavailable-journal"
    wal.unlink()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO grants(id,client_id,resource,scope,project,uid,auth_time,created,expires) VALUES('fixture-grant','client','http://127.0.0.1:8081/mcp','auto-coder.read','fixture','operator',1,1,2)")
        connection.execute("INSERT INTO access_tokens(hash,grant_id,expires,scope) VALUES('fixture','fixture-grant',1,NULL)")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        validate_retained_oauth(settings)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT scope FROM access_tokens WHERE hash='fixture'").fetchone() == (None,)


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
def test_real_unreaped_zombie_does_not_hide_namespace_absence(tmp_path: Path) -> None:
    import select
    import time

    from auto_coder.management.fresh_host_observation import _assert_all_task_networks_absent

    child = subprocess.Popen([sys.executable, "-c", "import sys,os;print('ready',flush=True);sys.stdin.read(1);os._exit(0)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert child.stdout is not None and child.stdin is not None
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline() == b"ready\n"
        actual = Path("/proc") / str(child.pid)
        namespace = os.readlink(actual / "ns/net")
        # Only this real task is projected into the helper's injectable view;
        # this is not a claim about absence in the complete host namespace.
        (tmp_path / str(child.pid)).symlink_to(actual, target_is_directory=True)
        with pytest.raises(HostObservationUnavailable, match="still has a task"):
            _assert_all_task_networks_absent(tmp_path, namespace)
        child.stdin.close()
        deadline = time.monotonic() + 5
        while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            assert time.monotonic() < deadline, "real child did not exit"
            time.sleep(0.01)
        fields = (actual / "stat").read_text().rsplit(")", 1)[1].split()
        assert fields[0] == "Z" and int(fields[19]) > 0
        assert (actual / "task" / str(child.pid)).is_dir()
        with pytest.raises(FileNotFoundError):
            os.readlink(actual / "ns/net")
        _assert_all_task_networks_absent(tmp_path, namespace)
    finally:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def _task_stat(pid: int, state: str = "Z", token: str = "42") -> str:
    return f"{pid} (test child) " + " ".join([state] + ["0"] * 18 + [token])


@pytest.mark.parametrize("persistent", [False, True])
def test_network_observation_retries_complete_scan_but_refuses_persistent_uncertainty(monkeypatch: pytest.MonkeyPatch, persistent: bool) -> None:
    from auto_coder.management import fresh_host_observation as observation

    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(observation.os, "readlink", lambda path: "namespace")
    monkeypatch.setattr(observation, "_validate_host_proc_profile", lambda *args: None)
    monkeypatch.setattr(observation.time, "sleep", lambda _: None)
    calls = []

    def scan(root, namespace):
        calls.append((root, namespace))
        if persistent or len(calls) == 1:
            raise observation._TaskNamespaceUnavailable("task network namespace is unavailable")

    monkeypatch.setattr(observation, "_assert_all_task_networks_absent", scan)
    if persistent:
        with pytest.raises(HostObservationUnavailable, match="network namespace observation unavailable"):
            observation.assert_prior_network_unreferenced("net:[202]")
    else:
        observation.assert_prior_network_unreferenced("net:[202]")
    assert calls == [(Path("/proc"), "net:[202]")] * (3 if persistent else 2)


@pytest.mark.parametrize("prior_holder_appears", [False, True])
def test_network_observation_rechecks_all_tasks_after_transient_link_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_holder_appears: bool) -> None:
    from auto_coder.management import fresh_host_observation as observation

    task = tmp_path / "42" / "task" / "42"
    (task / "ns").mkdir(parents=True)
    (task / "stat").write_text(_task_stat(42, "R"))
    readlink = os.readlink
    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(observation.os, "readlink", lambda path, *args, **kwargs: "host" if str(path).startswith("/proc/") else readlink(path, *args, **kwargs))
    profiles = []
    monkeypatch.setattr(observation, "_validate_host_proc_profile", lambda *args: profiles.append(args))
    scans = []
    scan = observation._assert_all_task_networks_absent

    def complete_scan(root, namespace):
        scans.append((root, namespace))
        return scan(tmp_path, namespace)

    delays = []

    def settle_exit(delay):
        delays.append(delay)
        (task / "ns/net").symlink_to("net:[301]")
        if prior_holder_appears:
            sibling = tmp_path / "43" / "task" / "44" / "ns"
            sibling.mkdir(parents=True)
            (sibling / "net").symlink_to("net:[202]")

    monkeypatch.setattr(observation, "_assert_all_task_networks_absent", complete_scan)
    monkeypatch.setattr("time.sleep", settle_exit)
    if prior_holder_appears:
        with pytest.raises(HostObservationUnavailable) as caught:
            observation.assert_prior_network_unreferenced("net:[202]")
        assert "prior network namespace still has a task" in str(caught.value.__cause__)
    else:
        observation.assert_prior_network_unreferenced("net:[202]")
    assert scans == [(Path("/proc"), "net:[202]")] * 2
    assert len(profiles) == 2
    assert delays == [0.01]


@pytest.mark.parametrize("fault", ["uid", "pid_namespace", "user_namespace", "profile"])
def test_network_observation_revalidates_authority_before_rescan(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    from auto_coder.management import fresh_host_observation as observation

    scans = []
    delays = []
    profiles = []
    monkeypatch.setattr(observation.os, "geteuid", lambda: 1 if scans and fault == "uid" else 0)

    def namespace(path):
        changed = (fault == "pid_namespace" and str(path) == "/proc/self/ns/pid") or (fault == "user_namespace" and str(path) == "/proc/self/ns/user")
        return "changed" if scans and changed else "host"

    def profile(*args):
        profiles.append(args)
        if scans and fault == "profile":
            raise ValueError("procfs visibility is restricted")

    def missing(root, target):
        scans.append((root, target))
        raise observation._TaskNamespaceUnavailable("task network namespace is unavailable (pid=42, tid=44)")

    monkeypatch.setattr(observation.os, "readlink", namespace)
    monkeypatch.setattr(observation, "_validate_host_proc_profile", profile)
    monkeypatch.setattr(observation, "_assert_all_task_networks_absent", missing)
    monkeypatch.setattr(observation.time, "sleep", delays.append)
    with pytest.raises(HostObservationUnavailable):
        observation.assert_prior_network_unreferenced("net:[202]")
    assert len(scans) == 1
    assert len(profiles) == (2 if fault == "profile" else 1)
    assert delays == [0.01]


@pytest.mark.parametrize("failure", [PermissionError("denied"), ValueError("task identity is malformed"), OSError("listing failed"), HostObservationUnavailable("prior network namespace still has a task")])
def test_network_observation_does_not_retry_other_refusals(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    from auto_coder.management import fresh_host_observation as observation

    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(observation.os, "readlink", lambda path: "host")
    monkeypatch.setattr(observation, "_validate_host_proc_profile", lambda *args: None)
    calls = []

    def refuse(*args):
        calls.append(args)
        raise failure

    delays = []
    monkeypatch.setattr(observation, "_assert_all_task_networks_absent", refuse)
    monkeypatch.setattr(observation.time, "sleep", delays.append)
    with pytest.raises(HostObservationUnavailable) as caught:
        observation.assert_prior_network_unreferenced("net:[202]")
    assert caught.value.__cause__ is failure
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("fault", ["permission", "malformed", "zero_token"])
def test_missing_namespace_does_not_erase_hard_lifetime_proof_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    from auto_coder.management import fresh_host_observation as observation

    task = tmp_path / "42/task/42"
    (task / "ns").mkdir(parents=True)
    (task / "stat").write_text("42 (child) Z" if fault == "malformed" else _task_stat(42, token="0" if fault == "zero_token" else "42"))
    readlink = os.readlink
    original_open = os.open
    permission = PermissionError("controlled held-directory stat-open denial")

    def open_stat(path, flags, *args, **kwargs):
        if fault == "permission" and path == "stat":
            raise permission
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(observation.os, "open", open_stat)
    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(observation.os, "readlink", lambda path, *args, **kwargs: "host" if str(path).startswith("/proc/") else readlink(path, *args, **kwargs))
    monkeypatch.setattr(observation, "_validate_host_proc_profile", lambda *args: None)
    scan = observation._assert_all_task_networks_absent
    scans = []

    def complete_scan(root, namespace):
        scans.append((root, namespace))
        return scan(tmp_path, namespace)

    delays = []

    def task_exits_before_hypothetical_retry(delay):
        delays.append(delay)
        # The old classifier could erase the observed hard error by accepting
        # this later clean scan. No retry is permitted after that hard error.
        (task / "stat").unlink()
        (task / "ns").rmdir()
        task.rmdir()
        task.parent.rmdir()
        task.parent.parent.rmdir()

    monkeypatch.setattr(observation, "_assert_all_task_networks_absent", complete_scan)
    monkeypatch.setattr(observation.time, "sleep", task_exits_before_hypothetical_retry)
    with pytest.raises(HostObservationUnavailable) as caught:
        observation.assert_prior_network_unreferenced("net:[202]")
    assert len(scans) == 1
    assert delays == []
    causes = []
    cause = caught.value.__cause__
    while cause is not None:
        causes.append(cause)
        cause = cause.__cause__
    if fault == "permission":
        assert permission in causes
    else:
        assert any(isinstance(cause, ValueError) and "task lifetime evidence is incomplete" in str(cause) for cause in causes)


@pytest.mark.parametrize("fault_errno,persistent", [(errno.ESRCH, False), (errno.ESRCH, True), (errno.EACCES, False), (None, False)])
def test_missing_namespace_reobserves_only_explicit_task_disappearance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_errno: int | None, persistent: bool) -> None:
    from auto_coder.management import fresh_host_observation as observation

    task = tmp_path / "42/task/44"
    (task / "ns").mkdir(parents=True)
    (task / "stat").write_text(_task_stat(44))
    original_open = os.open
    readlink = os.readlink
    vanished = ProcessLookupError(fault_errno, "controlled held-task lifetime loss")

    def open_stat(path, flags, *args, **kwargs):
        if path == "stat":
            raise vanished
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(observation.os, "open", open_stat)
    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(observation.os, "readlink", lambda path, *args, **kwargs: "host" if str(path).startswith("/proc/") else readlink(path, *args, **kwargs))
    profiles = []
    monkeypatch.setattr(observation, "_validate_host_proc_profile", lambda *args: profiles.append(args))
    scan = observation._assert_all_task_networks_absent
    scans = []

    def complete_scan(root, namespace):
        scans.append((root, namespace))
        return scan(tmp_path, namespace)

    delays = []

    def task_exits(delay):
        delays.append(delay)
        if not persistent:
            (task / "stat").unlink()
            (task / "ns").rmdir()
            task.rmdir()
            task.parent.rmdir()
            task.parent.parent.rmdir()

    monkeypatch.setattr(observation, "_assert_all_task_networks_absent", complete_scan)
    monkeypatch.setattr(observation.time, "sleep", task_exits)
    if fault_errno == errno.ESRCH and not persistent:
        observation.assert_prior_network_unreferenced("net:[202]")
        assert len(scans) == len(profiles) == 2
        assert delays == [0.01]
    else:
        with pytest.raises(HostObservationUnavailable) as caught:
            observation.assert_prior_network_unreferenced("net:[202]")
        attempts = 3 if fault_errno == errno.ESRCH else 1
        assert len(scans) == len(profiles) == attempts
        assert delays == [0.01] * (attempts - 1)
        causes = []
        cause = caught.value.__cause__
        while cause is not None:
            causes.append(cause)
            cause = cause.__cause__
        assert vanished in causes
        assert any("pid=42, tid=44" in str(cause) for cause in causes)


@pytest.mark.parametrize("fault", ["live", "truncated", "missing", "unreadable", "bad_pid", "bad_task_id", "zero_token", "bad_token", "changed_token", "state_changes", "namespace_reappears"])
def test_unknown_task_is_never_waived_as_a_zombie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    from auto_coder.management.fresh_host_observation import _assert_all_task_networks_absent

    task = tmp_path / "42" / "task" / ("0" if fault == "bad_task_id" else "42")
    (task / "ns").mkdir(parents=True)
    state = task / "stat"
    state.write_text(_task_stat(42))
    if fault == "live":
        state.write_text(_task_stat(42, "S"))
    elif fault == "truncated":
        state.write_text("42 (test child) Z")
    elif fault == "missing":
        state.unlink()
    elif fault == "bad_pid":
        state.write_text(_task_stat(43))
    elif fault in {"zero_token", "bad_token"}:
        state.write_text(_task_stat(42, token="0" if fault == "zero_token" else "unknown"))
    if fault == "bad_task_id":
        state.write_text(_task_stat(0))
    if fault == "unreadable":
        original_open = os.open

        def unavailable(path, flags, *args, **kwargs):
            if path == "stat":
                raise PermissionError("controlled unreadable task state")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", unavailable)
    original = os.readlink

    def observe(path, *, dir_fd=None):
        # Faults occur after the first state read, at the held-directory read.
        if dir_fd is not None:
            if fault == "changed_token":
                state.write_text(_task_stat(42, token="43"))
            elif fault == "state_changes":
                state.write_text(_task_stat(42, "S"))
            elif fault == "namespace_reappears":
                (task / "ns/net").symlink_to("net:[202]")
        return original(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", observe)
    with pytest.raises(ValueError, match="network namespace is unavailable"):
        _assert_all_task_networks_absent(tmp_path, "net:[202]")


def test_zombie_leader_does_not_hide_a_live_nonleader_namespace(tmp_path: Path) -> None:
    from auto_coder.management.fresh_host_observation import _assert_all_task_networks_absent

    leader = tmp_path / "42/task/42"
    worker = tmp_path / "42/task/43"
    (leader / "ns").mkdir(parents=True)
    (worker / "ns").mkdir(parents=True)
    (leader / "stat").write_text(_task_stat(42))
    (worker / "ns/net").symlink_to("net:[202]")
    with pytest.raises(HostObservationUnavailable, match="still has a task"):
        _assert_all_task_networks_absent(tmp_path, "net:[202]")


@pytest.mark.parametrize("final_cause", ["missing-link", "ESRCH"])
def test_fresh_host_command_reports_the_final_incomplete_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], final_cause: str) -> None:
    from auto_coder.management import fresh_host as command
    from auto_coder.management import fresh_host_observation as observation

    intent = _intent(tmp_path)
    fence = tmp_path / "held-fence"
    fence.write_bytes(b"")
    monkeypatch.setattr(command, "load_fresh_initialization", lambda *args, **kwargs: (intent, None))
    monkeypatch.setattr(command, "_evidence", lambda path: (None, None, intent.prior_evidence_sha256))
    monkeypatch.setattr("auto_coder.management.exclusion_host.validate_successor", lambda *args: ())
    monkeypatch.setattr(command, "acquire_prior_owner_fence", lambda *args, **kwargs: os.open(fence, os.O_RDONLY))
    # Only unrelated guard preconditions/listener setup are outside this unit
    # boundary. The command executes the production bounded observer/refusal.
    monkeypatch.setattr(command, "serve_one_fresh_grant", lambda *args, **kwargs: observation.assert_prior_network_unreferenced("net:[202]"))
    monkeypatch.setattr(sys, "argv", ["fresh-host", "guard", "--state-root", str(tmp_path / "state"), "--evidence-file", str(tmp_path / "evidence"), "--prior-lock", str(fence), "--guard-dir", str(tmp_path / "guard"), "--prior-network-fd", "123"])
    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(observation.os, "readlink", lambda path: "host")
    profiles = []
    monkeypatch.setattr(observation, "_validate_host_proc_profile", lambda *args: profiles.append(args))
    scans = []
    delays = []
    monkeypatch.setattr(observation.time, "sleep", delays.append)

    def incomplete(root, namespace):
        pid, tid = [(42, 43), (55, 56), (66, 67)][len(scans)]
        scans.append((root, namespace))
        if len(scans) == 3 and final_cause == "ESRCH":
            raise observation._TaskNamespaceUnavailable(f"task lifetime disappeared during namespace proof (pid={pid}, tid={tid}; ESRCH)") from ProcessLookupError(errno.ESRCH, "task exited")
        raise observation._TaskNamespaceUnavailable(f"task network namespace is unavailable (pid={pid}, tid={tid})")

    monkeypatch.setattr(observation, "_assert_all_task_networks_absent", incomplete)
    with pytest.raises(SystemExit) as caught:
        command.main()
    assert caught.value.code == 1
    assert len(scans) == len(profiles) == 3
    assert delays == [0.01, 0.01]
    diagnostic = capsys.readouterr().err.rstrip().splitlines()[-1]
    assert diagnostic.startswith("fresh host observation unavailable: network namespace observation unavailable")
    assert "pid=66, tid=67" in diagnostic
    assert ("ESRCH" if final_cause == "ESRCH" else "task network namespace is unavailable") in diagnostic
    assert not (tmp_path / "state/fresh-initialization.json").exists()
