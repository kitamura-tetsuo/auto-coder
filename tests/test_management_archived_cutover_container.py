"""Disposable real-container second cutover; no deployed credentials or services.

The host runner must be root. All Docker objects are UUID-named test objects.
The normal management child, host grant and kernel ownership checks are unpatched.
"""

from __future__ import annotations

import base64
import fcntl
import gc
import hashlib
import json
import os
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
import yaml

from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.exclusion_host import required_exclusions
from auto_coder.management.fresh_host import _write_evidence
from auto_coder.management.fresh_host_observation import capture_prior_container
from auto_coder.management.fresh_initialization import FreshInitializationIntent
from auto_coder.management.identity import AdmissionResult, AdmissionStatus, ReasonCode, VerifiedPrincipal
from auto_coder.management.mcp_runtime_tools import ManagementRuntime
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import AuthorizationRequest, OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, EffectReceipt, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.operating_policy import MANAGEMENT_REPOSITORY
from tests.test_management_fresh_end_to_end import _build_fixture_image, _fixture_executable_digest, _resolve_fixture_base

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


@dataclass
class Generation:
    name: str
    root: Path
    container: str
    mounts: list[tuple[Path, str, bool]]
    intent: Path | None = None
    network_fd: int | None = None
    prior_lock: Path | None = None
    proxy: str | None = None

    def path(self, name: str) -> Path:
        return self.root / name


class Chain:
    def __init__(self, root: Path):
        self.root = root
        self.names: list[str] = []
        self.generations: list[Generation] = []
        self.processes: list[subprocess.Popen] = []
        self.fds: list[int] = []
        self.tag = "auto-coder-ancestor-test:" + uuid.uuid4().hex
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.oauth = root / "oauth"
        self.oauth.mkdir(mode=0o700)
        self.origin = "http://127.0.0.1:8081"
        base = _resolve_fixture_base(os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544"))
        self.executable_digest = _fixture_executable_digest(base)
        context = root / "image"
        context.mkdir()
        source = Path(__file__).resolve().parents[1] / "src" / "auto_coder"
        shutil.copytree(source, context / "auto_coder", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _build_fixture_image(context, base, self.tag, owner_token_gap=False)
        self.image = self.docker("image", "inspect", self.tag, "--format", "{{.Id}}")
        probe = "from pathlib import Path;from auto_coder.management.fresh_initialization import application_tree_sha256;print(application_tree_sha256(Path('/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder')))"
        self.application_digest = self.docker("run", "--rm", "--network", "none", "--entrypoint", "python", self.image, "-c", probe).splitlines()[-1]

    def authorize(self) -> None:
        scopes = (READ_SCOPE, DEPLOY_SCOPE)
        client = OAuthClient("ancestor-client", (self.origin + "/callback",), allowed_scopes=scopes)
        settings = OAuthSettings(self.oauth / "oauth.sqlite3", self.origin, "ancestor-fixture", (client,), enabled_scopes=scopes)
        permitted = AdmissionResult(AdmissionStatus.PERMITTED, ReasonCode.PERMITTED, VerifiedPrincipal("ancestor-fixture", "operator", time.time() - 10))

        class LocalIdentity:
            def verify_login(self, _token):
                return permitted

            def recheck_identity(self, _principal):
                return permitted

            def check_operator_policy(self, _principal):
                return permitted

        grants = OAuthGrantService(settings, LocalIdentity())
        verifier = "v" * 48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        pending = grants.begin_authorization(AuthorizationRequest(client.client_id, self.origin + "/callback", settings.resource, challenge, scopes), "browser", "csrf")
        code = grants.approve_authorization(pending.request_id, "browser", "csrf", "synthetic-login").code
        assert code is not None
        self.token = grants.exchange_code(client.client_id, code, self.origin + "/callback", settings.resource, verifier).access_token
        grant = grants.authenticate_access(self.token, settings.resource, scopes)
        self.authority = ManagementRuntime._journal_authority(grant, grants.accepted_authority(grant))
        gc.collect()
        self.checkpoint(self.oauth / "oauth.sqlite3")
        (self.oauth / "allowed_uids.json").write_text(json.dumps({"project_id": "ancestor-fixture", "allowed_uids": ["operator"]}))
        self.grants_before = self.grants()

    @staticmethod
    def docker(*args: str, timeout: int = 60) -> str:
        return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.STDOUT, timeout=timeout).strip()

    @staticmethod
    def checkpoint(database: Path) -> None:
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    def grants(self):
        with sqlite3.connect(self.oauth / "oauth.sqlite3") as connection:
            return {name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in ("authority", "grants", "access_tokens", "refresh_tokens")}

    def prepare(self, name: str, prior: Generation | None = None, *, omit_archive: bool = False) -> Generation:
        root = self.root / name
        root.mkdir(mode=0o700)
        for part in ("state", "journal", "guard", "bootstrap"):
            (root / part).mkdir(mode=0o700)
        runtime = {
            "reference": "bootstrap",
            "receipt": "sha256:" + self.executable_digest,
            "executable": "/opt/auto-coder/.venv/bin/auto-coder",
            "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"],
            "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
            "drain_ack": "/management-child-state/drain-bootstrap.json",
        }
        config = {
            "instance_id": name,
            "state_root": "/management-child-state",
            "journal_root": "/management-operations",
            "oauth_root": "/management-oauth",
            "listener_url": self.origin + "/mcp",
            "initial_runtime": "bootstrap",
            "runtimes": [runtime],
            "restart_after_clean_shutdown": True,
            "crash_budget": 0,
        }
        captured = None
        proxy = None
        bindings = ()
        if prior is not None:
            captured = capture_prior_container(prior.container, self.image, expected_mounts=tuple((str(a), b, c) for a, b, c in prior.mounts), require_owner_lock=True, require_deployment_identity=True)
            proxy_name = "ancestor-proxy-" + uuid.uuid4().hex[:16]
            self.names.append(proxy_name)
            prior.proxy = self.docker("run", "-d", "--name", proxy_name, "--network", "container:" + prior.container, "--entrypoint", "/bin/sleep", self.image, "600")
            proxy = capture_prior_container(prior.proxy, self.image)
            bindings = required_exclusions(captured)
            config.update(fresh_guard_path="/management-initialization/fresh-guard.sock", fresh_prior_lock_path="/management-prior-owner.lock", archived_exclusions=[] if omit_archive else [asdict(item) for item in bindings])
        config_file = root / "bootstrap" / "config.json"
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        management = root / "bootstrap" / "management.yaml"
        management.write_text(
            yaml.safe_dump(
                {
                    "state_root": "/management-oauth",
                    "operation_state_root": "/management-operations",
                    "bootstrap_config_path": "/bootstrap/config.json",
                    "public_origin": self.origin,
                    "firebase_project_id": "ancestor-fixture",
                    "clients": [{"client_id": "ancestor-client", "redirect_uris": [self.origin + "/callback"], "allowed_scopes": [READ_SCOPE, DEPLOY_SCOPE]}],
                    "enabled_scopes": [READ_SCOPE, DEPLOY_SCOPE],
                    "bind_host": "127.0.0.1",
                    "port": 8081,
                    "local_development": True,
                }
            )
        )
        mounts = [(root / "state", "/management-child-state", True), (root / "journal", "/management-operations", True), (self.oauth, "/management-oauth", True), (root / "bootstrap", "/bootstrap", False)]
        if prior is not None:
            mounts += [(root / "guard", "/management-initialization", False), (prior.path("state") / ".supervisor-owner.lock", "/management-prior-owner.lock", False), (prior.path("journal"), "/management-prior-operations", False)]
            if not omit_archive:
                for item in bindings:
                    mounts += [(Path(item.host_journal_root), item.journal_root, False), (Path(item.host_fence), item.fence, False)]
        container_name = "ancestor-fixture-" + uuid.uuid4().hex[:16]
        self.names.append(container_name)
        args = ["create", "--name", container_name, "--read-only", "--publish", f"127.0.0.1:{self.port}:8080"]
        for source, target, writable in mounts:
            args += ["--mount", f"type=bind,src={source},dst={target}" + ("" if writable else ",readonly")]
        args += [self.image, "--config", "/bootstrap/config.json"]
        if prior is not None:
            args += ["--fresh-initialization"]
        generation = Generation(name, root, self.docker(*args), mounts)
        self.generations.append(generation)
        if captured is not None:
            generation.network_fd = os.open(f"/proc/{captured.pid}/ns/net", os.O_RDONLY)
            self.fds.append(generation.network_fd)
            digest = _write_evidence(root / "guard", {"management": asdict(captured), "proxy": asdict(proxy)})
            generation.prior_lock = prior.path("state") / ".supervisor-owner.lock"
            state, journal, oauth, guard, lock, db = (path.stat() for path in (root / "state", root / "journal", self.oauth, root / "guard", generation.prior_lock, prior.path("journal") / "management-operations.sqlite3"))
            intent = FreshInitializationIntent(
                str(uuid.uuid4()),
                prior.name,
                prior.container,
                name,
                generation.container,
                digest,
                lock.st_dev,
                lock.st_ino,
                self.port,
                hashlib.sha256(config_file.read_bytes()).hexdigest(),
                hashlib.sha256(management.read_bytes()).hexdigest(),
                self.image,
                self.executable_digest,
                self.application_digest,
                "bootstrap",
                "/management-child-state",
                "/management-operations",
                "/management-oauth",
                str(root / "state"),
                str(root / "journal"),
                str(self.oauth),
                state.st_dev,
                state.st_ino,
                journal.st_dev,
                journal.st_ino,
                oauth.st_dev,
                oauth.st_ino,
                guard.st_dev,
                guard.st_ino,
                str(prior.path("journal")),
                "/management-prior-operations",
                db.st_dev,
                db.st_ino,
            )
            generation.intent = root / "intent.json"
            generation.intent.write_text(json.dumps(asdict(intent)))
            generation.intent.chmod(0o600)
        return generation

    def command(self, generation: Generation) -> list[str]:
        assert generation.intent is not None and generation.network_fd is not None and generation.prior_lock is not None
        return [
            sys.executable,
            "-m",
            "auto_coder.management.exclusion_cutover",
            "--intent-file",
            str(generation.intent),
            "--evidence-file",
            str(generation.path("guard") / "prior-evidence.json"),
            "--bootstrap-config",
            str(generation.path("bootstrap") / "config.json"),
            "--prior-lock",
            str(generation.prior_lock),
            "--prior-network-fd",
            str(generation.network_fd),
            "--guard-dir",
            str(generation.path("guard")),
            "--wait-seconds",
            "60",
        ]

    def host(self, generation: Generation) -> subprocess.Popen:
        process = subprocess.Popen(self.command(generation), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(generation.network_fd,))
        self.processes.append(process)
        return process

    @staticmethod
    def status(process: subprocess.Popen, expected: str) -> None:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 60)[0], "host status timed out"
        line = process.stdout.readline()
        assert line, process.stderr.read() if process.stderr is not None else "missing host status"
        assert json.loads(line)["status"] == expected

    def stop(self, generation: Generation) -> None:
        self.docker("stop", "--time", "40", *([generation.proxy] if generation.proxy else []), generation.container, timeout=50)
        self.checkpoint(generation.path("journal") / "management-operations.sqlite3")

    def call(self, generation: Generation, tool: str, arguments: dict[str, object]):
        code = (
            "import json,sys,urllib.request; p=json.load(sys.stdin); "
            "r=urllib.request.Request('http://127.0.0.1:8081/mcp',data=json.dumps(p['request']).encode(),"
            "headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':'Bearer '+p['token']});"
            "print(urllib.request.urlopen(r,timeout=5).read().decode())"
        )
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        result = subprocess.run(["docker", "exec", "--interactive", generation.container, "python", "-c", code], input=json.dumps({"request": request, "token": self.token}), capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError("synthetic authenticated HTTP unavailable")
        return json.loads(result.stdout)["result"]["structuredContent"]

    def ready(self, generation: Generation, *, fresh: bool) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                observation = ""
                if fresh:
                    code = "import json;from pathlib import Path;from auto_coder.management.fresh_initialization import read_fresh_status;print(json.dumps(read_fresh_status(Path('/management-child-state'))))"
                    status = json.loads(self.docker("exec", generation.container, "python", "-c", code, timeout=10).splitlines()[-1])
                    if status["status"] not in {"launched", "succeeded"}:
                        time.sleep(0.1)
                        continue
                    observation = status["observation_id"]
                result = self.call(generation, "auto_coder_whoami", {"observation_id": observation})
                if result["uid"] == "operator" and (not fresh or status["status"] == "succeeded"):
                    return
            except (subprocess.CalledProcessError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        raise AssertionError("exact synthetic child readiness was not proven")

    @staticmethod
    def child(generation: Generation):
        with sqlite3.connect(generation.path("state") / "management-child.sqlite3") as connection:
            return json.loads(connection.execute("SELECT record_json FROM child_state").fetchone()[0])

    def prove_child(self, generation: Generation) -> None:
        observation = uuid.uuid4().hex
        started = time.time()
        assert self.call(generation, "auto_coder_whoami", {"observation_id": observation})["uid"] == "operator"
        code = (
            "import sys;from pathlib import Path;"
            "from auto_coder.management.bootstrap_supervisor import BootstrapConfig;"
            "from auto_coder.management.readiness_observation import read_authenticated_readback;"
            "c=BootstrapConfig.read(Path('/bootstrap/config.json'));"
            "p=read_authenticated_readback(c,observation_id=sys.argv[1],observation_started_at=float(sys.argv[2]),observation_ends_at=float(sys.argv[2])+60);"
            "print(p.child_identity if p else 'UNAVAILABLE')"
        )
        proof = self.docker("exec", generation.container, "python", "-c", code, observation, str(started)).splitlines()[-1]
        child = self.child(generation)
        assert proof == f"{child['pid']}:{child['start_token']}:{child['child_generation']}:{child['selected_runtime']}:{child['supervisor_incarnation']}"

    def start_cutover(self, old: Generation, new: Generation) -> None:
        process = self.host(new)
        self.status(process, "ancestors-fenced-awaiting-manual-stop")
        self.stop(old)
        self.status(process, "awaiting-replacement")
        self.docker("start", new.container)
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, (out, err)
        self.ready(new, fresh=True)
        self.prove_child(new)

    def seed(self, generation: Generation):
        store = ManagementOperationStore(generation.path("journal"))
        intent = OperationIntent(ManagedTarget(generation.name, "management", MANAGEMENT_REPOSITORY), "management-service-self-update", (ActionParameter("source_sha", "a" * 40),), "old-key-" + generation.name)
        record = store.admit(intent, self.authority).record
        assert record is not None
        assert store.transition(record.operation_id, 0, OperationPhase.FAILED, evidence=EffectReceipt(record.operation_id, "synthetic-no-effect", 1, "no_effect_sent")).record is not None
        self.checkpoint(generation.path("journal") / "management-operations.sqlite3")
        return record

    @staticmethod
    def snapshot(generation: Generation):
        paths = [generation.path("journal") / "management-operations.sqlite3", generation.path("bootstrap") / "config.json"]
        intent = generation.path("state") / "fresh-initialization.json"
        if intent.exists():
            paths.append(intent)
        return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    def owner_refused(self, generation: Generation) -> None:
        # No published port: refusal must be the actual owner lock, not bind conflict.
        args = ["run", "--rm", "--network", "none"]
        for source, target, writable in generation.mounts:
            args += ["--mount", f"type=bind,src={source},dst={target}" + ("" if writable else ",readonly")]
        before = (generation.path("state") / "management-child.sqlite3").read_bytes()
        result = subprocess.run(["docker", *args, self.image, "--config", "/bootstrap/config.json"], capture_output=True, text=True, timeout=30)
        assert result.returncode != 0 and "BlockingIOError" in result.stdout + result.stderr
        assert (generation.path("state") / "management-child.sqlite3").read_bytes() == before

    def close(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
        for fd in self.fds:
            os.close(fd)
        if self.names:
            subprocess.run(["docker", "rm", "-f", *self.names], capture_output=True, timeout=60)
        subprocess.run(["docker", "image", "rm", self.tag], capture_output=True, timeout=30)


@pytest.fixture
def chain(tmp_path: Path):
    if os.geteuid() != 0:
        pytest.skip("host root is required for disposable namespace and proxy evidence")
    fixture = Chain.__new__(Chain)
    try:
        fixture.__init__(tmp_path)
        yield fixture
    finally:
        fixture.close()


@pytest.mark.parametrize("boundary", ["complete", "host-interrupted", "omitted-ancestor", "stopped-prior"])
def test_real_second_cutover_preserves_all_owner_and_key_exclusions(chain: Chain, boundary: str) -> None:
    a = chain.prepare("manager-a")
    chain.docker("start", a.container)
    deadline = time.monotonic() + 30
    while True:
        try:
            with sqlite3.connect((a.path("state") / "management-child.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute("SELECT record_json FROM child_state").fetchone()
                record = json.loads(row[0]) if row else {}
            if record.get("phase") == "running" and record.get("pid", 0) > 0:
                break
        except sqlite3.OperationalError:
            pass
        assert time.monotonic() < deadline, "first owner did not launch its actual management child"
        time.sleep(0.05)
    chain.authorize()
    chain.ready(a, fresh=False)
    old_a = chain.seed(a)
    b = chain.prepare("manager-b", a)
    chain.start_cutover(a, b)
    old_b = chain.seed(b)
    original_a = chain.snapshot(a)
    c = chain.prepare("manager-c", b, omit_archive=boundary == "omitted-ancestor")
    if boundary in {"omitted-ancestor", "stopped-prior"}:
        if boundary == "stopped-prior":
            chain.stop(b)
        result = subprocess.run(chain.command(c), pass_fds=(c.network_fd,), capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert not (c.path("state") / "fresh-initialization.json").exists()
        assert not (c.path("state") / "management-child.sqlite3").exists()
        assert not list(c.path("journal").iterdir())
    elif boundary == "host-interrupted":
        process = chain.host(c)
        chain.status(process, "ancestors-fenced-awaiting-manual-stop")
        chain.stop(b)
        chain.status(process, "awaiting-replacement")
        claim = (c.path("state") / "fresh-initialization.json").read_bytes()
        process.terminate()
        process.communicate(timeout=10)
        retried = subprocess.run(chain.command(c), pass_fds=(c.network_fd,), capture_output=True, text=True, timeout=30)
        assert retried.returncode != 0 and (c.path("state") / "fresh-initialization.json").read_bytes() == claim
        chain.docker("start", c.container)
        assert chain.docker("wait", c.container, timeout=30) != "0"
        assert not (c.path("state") / "fresh-launched.json").exists()
        assert not (c.path("state") / "fresh-launch-claimed.json").exists()
        ledger = c.path("state") / "management-child.sqlite3"
        if ledger.exists():
            with sqlite3.connect(ledger) as db:
                assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM child_state").fetchone()[0] == 0
        assert not list(c.path("journal").iterdir())
    else:
        chain.start_cutover(b, c)
        original_b = chain.snapshot(b)
        for previous in (a, b):
            chain.owner_refused(previous)
        for old in (old_a, old_b):
            response = chain.call(c, "auto_coder_update_management_service", {"source_sha": "a" * 40, "idempotency_key": old.intent.idempotency_key})
            if old is old_a:
                assert response["status"] == "CONFLICT" and response.get("operation_id") is None
                assert chain.call(c, "auto_coder_operation_get", {"operation_id": old.operation_id})["status"] == "NOT_FOUND"
            else:
                assert response["operation_id"] == old.operation_id and response["history_origin"] == "retained_prior_instance"
        with sqlite3.connect(c.path("journal") / "management-operations.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        # Preserve host fences across the deliberate clean C reconstruction.
        held = []
        try:
            for previous in (a, b):
                fd = os.open(previous.path("state") / ".supervisor-owner.lock", os.O_RDONLY)
                held.append(fd)
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            before = chain.child(c)
            chain.stop(c)
            chain.docker("start", c.container)
            chain.ready(c, fresh=True)
            after = chain.child(c)
            assert after["child_generation"] == before["child_generation"] + 1
            assert after["supervisor_incarnation"] != before["supervisor_incarnation"]
            chain.prove_child(c)
        finally:
            for fd in held:
                os.close(fd)
        for previous in (a, b):
            chain.owner_refused(previous)
        assert chain.snapshot(b) == original_b
        response = chain.call(c, "auto_coder_update_management_service", {"source_sha": "a" * 40, "idempotency_key": old_a.intent.idempotency_key})
        assert response["status"] == "CONFLICT"
    assert chain.snapshot(a) == original_a
    assert chain.grants() == chain.grants_before
