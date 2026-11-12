"""Build an exact-source quarantined original on an exclusively disposable host.

This fixture never grants a fresh launch. It genuinely stages a public tested
runtime, lets the unmodified pinned bootstrap publish its own owner/ledger and
request a grant, then suspends that actual process while the caller captures its
kernel identity. No READY, supervisor row or successful outcome is fabricated.
The full host driver owns VM isolation, Caddy, enrollment and observation.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from loguru import logger
from scripts.test_management_containment_enrollment_host import _private_directory, _write_private

from auto_coder.management.containment_kernel_subobservation import _holder_start_token, capture_quarantined_original_container
from auto_coder.management.fresh_host import _evidence
from auto_coder.management.fresh_host_observation import PriorContainer
from auto_coder.management.fresh_initialization import FreshInitializationIntent, claim_fresh_initialization
from auto_coder.management.fresh_oauth import validate_retained_oauth
from auto_coder.management.installed_bootstrap_source import _REVIEWED_QUARANTINED_SOURCE_SHA256
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings, load_management_settings
from auto_coder.management.oauth_store import OAuthStore

QUARANTINE_REVISION = "1b23f3be30ca47926462e7d261ef98e79bb82728"
QUARANTINE_IMAGE = "sha256:bf88d2ea40f894d4bbc308be1694d8a1ed5dcf2d890a8606c4a6f6376ab5c750"
_BOOTSTRAP_SOURCE = "src/auto_coder/management/bootstrap_supervisor.py"

_IMAGE_PROBE = """import hashlib,json,subprocess,sys
from pathlib import Path
import auto_coder
from auto_coder.management.fresh_initialization import application_tree_sha256
root=Path(auto_coder.__file__).resolve().parent
print(json.dumps({
 'application_root':str(root),
 'application_sha256':application_tree_sha256(root),
 'bootstrap_sha256':hashlib.sha256((root/'management/bootstrap_supervisor.py').read_bytes()).hexdigest(),
 'console_sha256':hashlib.sha256(Path('/opt/auto-coder/.venv/bin/auto-coder').read_bytes()).hexdigest(),
 'python_version':f'{sys.version_info.major}.{sys.version_info.minor}',
 'uv_version':subprocess.check_output(['/usr/local/bin/uv','--version'],text=True).strip()
},sort_keys=True))
"""

_PREPARE_RUNTIME = """import json,sys
from dataclasses import asdict
from pathlib import Path
from auto_coder.management.bootstrap_supervisor import BootstrapConfig
from auto_coder.management.configured_stager import configured_management_stager
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.self_update_handoff import runtime_for_receipt
from auto_coder.management.version_staging import ManagementPrepareIntent
config=BootstrapConfig.read(Path('/bootstrap/config.json'))
settings=load_management_settings(Path('/bootstrap/management.yml'))
# Use the production public GitHub authority without any transport override.
stager=configured_management_stager(settings,config)
ready=stager.prepare(ManagementPrepareIntent(sys.argv[1],'containment-fixture')).receipt
runtime=runtime_for_receipt(config,ready)
print(json.dumps({'receipt':asdict(ready),'runtime':{**asdict(runtime),'environment':dict(runtime.environment)}},default=str,sort_keys=True))
"""


class DockerRunner(Protocol):
    def __call__(self, *arguments: str, timeout: int = 30) -> str: ...


@dataclass(frozen=True)
class HeldFreshRequest:
    request_id: str
    intent_sha256: str
    owner_incarnation: str
    new_container_id: str
    challenge: str


class HeldFreshGuard:
    """Receive one genuine request and suspend its exact peer without replying."""

    def __init__(self, path: Path, *, request_id: str, intent_sha256: str, container_id: str) -> None:
        self.path = path
        self.expected = (request_id, intent_sha256, container_id)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection: socket.socket | None = None
        self.pidfd = -1
        self.request: HeldFreshRequest | None = None
        try:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                # The private fixture root can exceed AF_UNIX's pathname bound.
                # Bind through its actual directory FD; clients use the same
                # socket inode through the mounted short container pathname.
                self.listener.bind(f"/proc/self/fd/{directory}/{path.name}")
            finally:
                os.close(directory)
            path.chmod(0o600)
            self.listener.listen(1)
        except BaseException:
            self.listener.close()
            raise

    def receive_and_pause(self, expected_pid: int, *, expected_start_token: str, timeout: float = 180) -> HeldFreshRequest:
        if expected_pid <= 0 or self.request is not None:
            raise ValueError("the fixture requires one actual live manager peer")
        self.pidfd = os.pidfd_open(expected_pid, 0)
        if _holder_start_token(expected_pid) != expected_start_token:
            raise ValueError("the expected manager lifetime changed before the real request")
        self.listener.settimeout(timeout)
        self.connection, _ = self.listener.accept()
        self.connection.settimeout(5)
        pid, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if (pid, uid) != (expected_pid, os.geteuid()):
            raise ValueError("fresh request peer differs from the actual Docker init")
        with self.connection.makefile("rb") as stream:
            line = stream.readline(4097)
        if len(line) > 4096 or not line.endswith(b"\n"):
            raise ValueError("the real fresh request is incomplete")
        raw = json.loads(line)
        if not isinstance(raw, dict) or set(raw) != set(HeldFreshRequest.__dataclass_fields__) or any(not isinstance(value, str) for value in raw.values()):
            raise ValueError("the real fresh request has unsupported fields")
        request = HeldFreshRequest(**raw)
        if (request.request_id, request.intent_sha256, request.new_container_id) != self.expected or str(uuid.UUID(request.owner_incarnation)) != request.owner_incarnation or len(request.challenge) != 64 or any(char not in "0123456789abcdef" for char in request.challenge):
            raise ValueError("the real fresh request differs from the sealed intent")
        signal.pidfd_send_signal(self.pidfd, signal.SIGSTOP)
        deadline = time.monotonic() + 2
        while True:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "T":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("the actual fresh owner did not stop at the held request")
            time.sleep(0.01)
        self.request = request
        return request

    def close(self) -> None:
        # Never send a grant, resume a manager, or manufacture launch records.
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.listener.close()
        if self.pidfd >= 0:
            os.close(self.pidfd)
            self.pidfd = -1
        self.path.unlink(missing_ok=True)


@dataclass
class PreparedQuarantine:
    root: Path
    manager_id: str
    image_id: str
    bootstrap_config: Path
    management_config: Path
    state_root: Path
    journal_root: Path
    oauth_root: Path
    mounts: tuple[tuple[str, str, bool], ...]
    support_source_descriptor: Path
    guard: HeldFreshGuard
    docker: DockerRunner = field(repr=False)
    prior: PriorContainer | None = None
    stopped: bool = False

    def start_and_capture(self) -> PriorContainer:
        """Capture only after the pinned bootstrap reaches its actual grant RPC."""
        if self.prior is not None or self.stopped:
            raise RuntimeError("the quarantined fixture lifetime cannot be replayed")
        self.docker("start", self.manager_id)
        current = json.loads(self.docker("inspect", "--type", "container", self.manager_id))[0]
        state = current.get("State", {})
        if current.get("Id") != self.manager_id or current.get("Image") != self.image_id or state.get("Running") is not True or type(state.get("Pid")) is not int:
            raise RuntimeError("the exact quarantined manager did not start")
        request = self.guard.receive_and_pause(state["Pid"], expected_start_token=_holder_start_token(state["Pid"]))
        prior = capture_quarantined_original_container(self.manager_id, self.image_id, expected_mounts=self.mounts, bootstrap_config=self.bootstrap_config)
        with closing(sqlite3.connect((self.state_root / "management-child.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            owners = connection.execute("SELECT instance_id,incarnation,pid,start_token FROM supervisor_presence").fetchall()
        if owners != [(prior.instance_id, request.owner_incarnation, 1, prior.start_token)]:
            raise AssertionError("the genuine guard requester differs from its published supervisor row")
        if any((self.state_root / name).exists() for name in ("fresh-launch-claimed.json", "fresh-launched.json", "fresh-outcome.json")):
            raise AssertionError("the held fixture unexpectedly crossed the first-launch boundary")
        self.prior = prior
        logger.info("Captured exact quarantined owner at its ungranted fresh request: {}", self.manager_id)
        return prior

    def stop(self) -> None:
        """Kill only this disposable held owner; keep its Docker object to read."""
        if self.stopped:
            return
        running = self.docker("inspect", "--format", "{{.State.Running}}", self.manager_id)
        if running == "true":
            self.docker("kill", "--signal", "KILL", self.manager_id)
        self.docker("wait", self.manager_id)
        if self.docker("inspect", "--format", "{{.State.Running}}", self.manager_id) != "false":
            raise AssertionError("the quarantined fixture manager is still live")
        self.guard.close()
        self.stopped = True

    def __enter__(self) -> PreparedQuarantine:
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.stop()
        finally:
            try:
                self.docker("rm", "--force", self.manager_id)
            finally:
                self.guard.close()


def _mount_arguments(mounts: tuple[tuple[str, str, bool], ...]) -> list[str]:
    result: list[str] = []
    for source, destination, writable in mounts:
        result.extend(("--mount", f"type=bind,src={source},dst={destination}" + ("" if writable else ",readonly")))
    return result


def prepare_quarantine(
    *,
    root: Path,
    name: str,
    image_id: str,
    source_repository: Path,
    predecessor: PriorContainer,
    prior_evidence_path: Path,
    prior_lock: Path,
    host_listener_port: int,
    docker: DockerRunner,
) -> PreparedQuarantine:
    """Prepare a genuine public tested runtime and a never-granted fresh owner.

    Prerequisites are an isolated root/systemd/Docker host, the pinned original
    image, a non-shallow Git repository containing QUARANTINE_REVISION, public GitHub
    Actions readback, and frozen uv dependency installation network access. No
    production credential is read or supplied to the installer or manager.
    The predecessor must be an actually captured fixture owner, already stopped;
    its retained evidence and real lock inode supply the fresh intent inputs.
    """
    if os.geteuid() != 0 or not name.startswith("ac-test-pr2829-") or not 0 < host_listener_port <= 65535:
        raise ValueError("quarantine setup requires the dedicated root fixture")
    predecessor.validate()
    prior, _, evidence_digest = _evidence(prior_evidence_path)
    if prior != predecessor or not predecessor.instance_id:
        raise ValueError("the fixture predecessor differs from real captured evidence")
    lock_info = prior_lock.lstat()
    if prior_lock.is_symlink() or not stat.S_ISREG(lock_info.st_mode) or (lock_info.st_dev, lock_info.st_ino) != (predecessor.owner_lock_device, predecessor.owner_lock_inode):
        raise ValueError("the real predecessor owner inode is unavailable")
    source_repository = source_repository.resolve(strict=True)
    shallow = subprocess.run(("git", "-C", str(source_repository), "rev-parse", "--is-shallow-repository"), check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if shallow != "false":
        raise ValueError("genuine tested-source staging requires complete source ancestry")
    source = subprocess.run(("git", "-C", str(source_repository), "show", f"{QUARANTINE_REVISION}:{_BOOTSTRAP_SOURCE}"), check=True, capture_output=True, timeout=30).stdout
    if hashlib.sha256(source).hexdigest() != _REVIEWED_QUARANTINED_SOURCE_SHA256:
        raise ValueError("the fixture source checkout lacks the pinned reviewed bootstrap")
    metadata = json.loads(docker("image", "inspect", image_id))[0]
    if metadata.get("Id") != image_id or metadata.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != QUARANTINE_REVISION:
        raise ValueError("the fixture image is not the exact pinned original revision")
    probe = json.loads(docker("run", "--rm", "--read-only", "--network", "none", "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", image_id, "-c", _IMAGE_PROBE).splitlines()[-1])
    if probe.get("bootstrap_sha256") != _REVIEWED_QUARANTINED_SOURCE_SHA256 or probe.get("python_version") != "3.12" or probe.get("uv_version") != "uv 0.7.22":
        raise ValueError("the original image source or frozen Python/uv profile differs")
    if not _private_directory(root) or any(root.iterdir()):
        raise ValueError("quarantine setup requires a fresh private fixture directory")
    bootstrap, state, journal, oauth, guard_root, versions = (root / value for value in ("bootstrap", "state", "journal", "oauth", "guard", "versions"))
    for directory in (bootstrap, state, journal, oauth, guard_root, versions):
        _private_directory(directory)
    management_path = bootstrap / "management.yml"
    issuer = f"http://127.0.0.1:{host_listener_port}"
    oauth_settings = OAuthSettings(oauth / "oauth.sqlite3", issuer, "containment-fixture", (OAuthClient("fixture-client", ("http://127.0.0.1:19001/callback",)),))
    previous_umask = os.umask(0o077)
    try:
        store = OAuthStore(oauth_settings)
        with closing(store.connect()) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        os.umask(previous_umask)
    validate_retained_oauth(oauth_settings)
    management = {
        "state_root": "/management-oauth",
        "public_origin": issuer,
        "firebase_project_id": "containment-fixture",
        "clients": [{"client_id": "fixture-client", "redirect_uris": ["http://127.0.0.1:19001/callback"]}],
        "bind_host": "127.0.0.1",
        "port": 8081,
        "local_development": True,
        "operation_state_root": "/management-operations",
        "bootstrap_config_path": "/bootstrap/config.json",
        "self_update_source_repository": "/management-source",
        "self_update_python_executable": "/opt/auto-coder/.venv/bin/python",
        "self_update_uv_executable": "/usr/local/bin/uv",
        "self_update_uv_version": "0.7.22",
    }
    _write_private(management_path, json.dumps(management, sort_keys=True).encode())
    load_management_settings(management_path)
    configuration: dict[str, object] = {
        "instance_id": name,
        "state_root": "/management-child-state",
        "oauth_root": "/management-oauth",
        "journal_root": "/management-operations",
        "listener_url": "http://127.0.0.1:8081/mcp",
        "initial_runtime": "bootstrap",
        "runtimes": [
            {
                "reference": "bootstrap",
                "receipt": "sha256:" + probe["console_sha256"],
                "executable": "/opt/auto-coder/.venv/bin/auto-coder",
                "arguments": ["management", "serve", "--config", "/bootstrap/management.yml"],
                "environment": {"AUTO_CODER_LOG_FILE": "/management-child-state/logs/child.log", "TMPDIR": "/management-child-state/logs"},
            }
        ],
        "management_version_root": "/management-versions",
        "management_config": "/bootstrap/management.yml",
        "fresh_guard_path": "/management-initialization/fresh-guard.sock",
        "fresh_prior_lock_path": "/management-prior-owner.lock",
        "graceful_timeout": 1,
        "crash_budget": 0,
    }
    staging_config = bootstrap / "staging-config.json"
    _write_private(staging_config, json.dumps(configuration, sort_keys=True).encode())
    logger.info("Preparing genuine tested-source runtime {} with unauthenticated public readback", QUARANTINE_REVISION)
    staged = docker(
        "run",
        "--rm",
        "--network",
        "bridge",
        "--env",
        "GITHUB_TOKEN=",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "UV_NO_CACHE=1",
        *_mount_arguments(((str(staging_config), "/bootstrap/config.json", False), (str(management_path), "/bootstrap/management.yml", False), (str(source_repository), "/management-source", False), (str(versions), "/management-versions", True))),
        "--entrypoint",
        "python",
        image_id,
        "-c",
        _PREPARE_RUNTIME,
        QUARANTINE_REVISION,
        timeout=1800,
    )
    prepared = json.loads(staged.splitlines()[-1])
    ready, runtime = prepared["receipt"], prepared["runtime"]
    if ready.get("source_sha") != QUARANTINE_REVISION or ready.get("instance_id") != name or runtime.get("execution_kind") != "management-installed":
        raise AssertionError("the genuine stager returned a different fixture runtime")
    configuration.update(initial_runtime=ready["management_runtime_id"], runtimes=[runtime], fresh_runtime_binding={"version": 2, "bootstrap_application_root": probe["application_root"], "initial_ready_path": runtime["ready_receipt"], "initial_ready_identity": ready["receipt_identity"]})
    config_path = bootstrap / "config.json"
    _write_private(config_path, json.dumps(configuration, sort_keys=True).encode())
    _write_private(root / "prepared-runtime.json", json.dumps(prepared, sort_keys=True).encode())
    support = root / "support-source.json"
    source_info = source_repository.stat()
    _write_private(
        support,
        json.dumps({"schema": "approved-quarantine-support-directory-v1", "purpose": "non-executed-bootstrap-reference", "source": str(source_repository), "destination": "/management-source", "read_only": True, "device": source_info.st_dev, "inode": source_info.st_ino}, sort_keys=True).encode(),
    )
    mounts = tuple(
        sorted(
            (
                (str(config_path), "/bootstrap/config.json", False),
                (str(management_path), "/bootstrap/management.yml", False),
                (str(source_repository), "/management-source", False),
                (str(versions), "/management-versions", False),
                (str(state), "/management-child-state", True),
                (str(journal), "/management-operations", True),
                (str(oauth), "/management-oauth", True),
                (str(guard_root), "/management-initialization", False),
                (str(prior_lock), "/management-prior-owner.lock", False),
            )
        )
    )
    container_id = docker(
        "create",
        "--name",
        name,
        "--label",
        f"auto-coder.containment-fixture={name}",
        "--read-only",
        "--security-opt",
        "no-new-privileges",
        "--restart",
        "no",
        "--publish",
        f"127.0.0.1:{host_listener_port}:8080",
        "--env",
        "GITHUB_TOKEN=",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        *_mount_arguments(mounts),
        "--entrypoint",
        "python",
        image_id,
        "-m",
        "auto_coder.management.bootstrap_supervisor",
        "--config",
        "/bootstrap/config.json",
        "--fresh-initialization",
    )
    try:
        s, j, o, g = (directory.stat() for directory in (state, journal, oauth, guard_root))
        intent = FreshInitializationIntent(
            str(uuid.uuid4()),
            predecessor.instance_id,
            predecessor.container_id,
            name,
            container_id,
            evidence_digest,
            lock_info.st_dev,
            lock_info.st_ino,
            host_listener_port,
            hashlib.sha256(config_path.read_bytes()).hexdigest(),
            hashlib.sha256(management_path.read_bytes()).hexdigest(),
            image_id,
            ready["python_executable_sha256"],
            probe["application_sha256"],
            ready["management_runtime_id"],
            "/management-child-state",
            "/management-operations",
            "/management-oauth",
            str(state),
            str(journal),
            str(oauth),
            s.st_dev,
            s.st_ino,
            j.st_dev,
            j.st_ino,
            o.st_dev,
            o.st_ino,
            g.st_dev,
            g.st_ino,
        )
        digest = claim_fresh_initialization(state, intent)
        guard = HeldFreshGuard(guard_root / "fresh-guard.sock", request_id=intent.request_id, intent_sha256=digest, container_id=container_id)
        return PreparedQuarantine(root, container_id, image_id, config_path, management_path, state, journal, oauth, mounts, support, guard, docker)
    except BaseException:
        docker("rm", "--force", container_id)
        raise
