"""Durable fresh-instance intent does not itself authorize a launch."""

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
