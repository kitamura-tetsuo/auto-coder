"""Disposable real-container second cutover; no deployed credentials or services.

The host runner must be root. All Docker objects are UUID-named test objects.
The normal management child, host grant and kernel ownership checks are unpatched.
"""

from __future__ import annotations

import base64
import fcntl
import gc
import hashlib
import http.client
import json
import os
import random
import re
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
import yaml

from auto_coder.management.action_scopes import DEPLOY_SCOPE, OPERATE_SCOPE, READ_SCOPE
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


def diagnostic_log_lines(content: str, token: str = "") -> list[str]:
    """Bounded error observations; no headers, bodies or credential strings."""
    result = []
    for line in content.splitlines()[-60:]:
        try:
            value = json.loads(line)
        except ValueError:
            if not any(word in line.lower() for word in ("error", "exception", "refus", "unavailable", "traceback", "fresh management", "read-only")):
                continue
        else:
            if not isinstance(value, dict):
                continue
            line = json.dumps({key: value[key] for key in ("level", "msg", "error") if isinstance(value.get(key), str)})
        if re.search(r"(?i)\b(headers?|body|authorization|cookie|token|secret|password)\b", line):
            continue
        if token:
            line = line.replace(token, "[redacted]")
        line = re.sub(r"(?i)(?:Bearer|Basic)\s+[^\s\"',}]+", "[redacted authorization]", line)
        line = re.sub(r"[A-Za-z0-9_+/.=~-]{24,}", "[redacted]", line)
        result.append(line[:1024])
    return result


def transport_failure(error: Exception) -> str:
    """Classify connection failure without persisting URL, headers or secrets."""
    if isinstance(error, urllib.error.HTTPError):
        return "HTTPError status=" + str(error.code)
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    number = getattr(cause, "errno", None)
    return type(error).__name__ + ":" + type(cause).__name__ + (" errno=" + str(number) if isinstance(number, int) else "")


def publish_diagnostics(directory: Path, document: dict) -> None:
    """Publish only sanitized observations to the invoking artifact owner.

    State, journals, OAuth and secret mounts are never chmod/chowned or copied.
    Keep the new artifact private; hand ownership back after the complete write.
    """
    parent = directory.parent.stat()
    uid, gid = parent.st_uid, parent.st_gid
    if os.geteuid() == 0 and os.environ.get("SUDO_UID", "").isdecimal() and os.environ.get("SUDO_GID", "").isdecimal():
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    directory.mkdir(mode=0o700)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        target = os.open("observations.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(target, "w") as stream:
            json.dump(document, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            current = os.fstat(stream.fileno())
            if (current.st_uid, current.st_gid) != (uid, gid):
                os.fchown(stream.fileno(), uid, gid)
        current = os.fstat(descriptor)
        if (current.st_uid, current.st_gid) != (uid, gid):
            os.fchown(descriptor, uid, gid)
    finally:
        os.close(descriptor)
    if os.geteuid() == 0 and uid != 0:
        # The actual Actions uploader is not root. Prove only the sanitized
        # artifact is readable under that identity, without forwarding env/data.
        probe = "from pathlib import Path;import json,sys;assert isinstance(json.loads(Path(sys.argv[1]).read_text()),dict);print('uploader-read-confirmed')"
        observed = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(directory / "observations.json")], user=uid, group=gid, extra_groups=[], cwd=directory.parent, env={"PATH": os.defpath}, capture_output=True, text=True, timeout=10, check=True)
        assert observed.stdout.strip() == "uploader-read-confirmed"
        print("cutover-uploader-read-confirmed uid=" + str(uid))


def worker_compose_profile(fixtures: Path, subnet: str, gateway: str):
    """Keep canonical isolation/worker configuration; pin its host bridge IP."""
    import ipaddress

    network = ipaddress.ip_network(subnet)
    address = ipaddress.ip_address(gateway)
    if address not in network.hosts() or address in {network.network_address + 11, network.network_address + 12}:
        raise ValueError("fixture gateway overlaps a worker or leaves its subnet")
    profile = yaml.safe_load((fixtures / "compose.supervisor-network.yml").read_text())
    profile["networks"]["private"]["ipam"]["config"] = [{"subnet": subnet, "gateway": gateway}]
    return profile


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
    def __init__(self, root: Path, *, with_workers: bool = False):
        self.root = root
        self.names: list[str] = []
        self.generations: list[Generation] = []
        self.processes: list[subprocess.Popen] = []
        self.fds: list[int] = []
        self.worker_environment = None
        self.worker_compose = None
        self.worker_baseline = None
        self.proxy_tag = "auto-coder-ancestor-caddy:" + uuid.uuid4().hex
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
        self.proxy_image = self.prepare_caddy_image()
        if with_workers:
            self.start_workers()
        probe = "from pathlib import Path;from auto_coder.management.fresh_initialization import application_tree_sha256;print(application_tree_sha256(Path('/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder')))"
        self.application_digest = self.docker("run", "--rm", "--network", "none", "--read-only", "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", self.image, "-c", probe).splitlines()[-1]

    def prepare_caddy_image(self) -> str:
        """Use the real pinned Caddy binary with the supported bind-only profile."""
        source = _resolve_fixture_base("caddy@sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489")
        binary = self.root / "caddy"
        donor = self.docker("create", "--network", "none", source)
        target = None
        try:
            self.docker("cp", donor + ":/usr/bin/caddy", str(binary))
            expected = self.docker("run", "--rm", "--network", "none", "--entrypoint", "sha256sum", source, "/usr/bin/caddy").split()[0]
            assert hashlib.sha256(binary.read_bytes()).hexdigest() == expected
            target = self.docker("create", "--network", "none", self.image)
            self.docker("cp", str(binary), target + ":/usr/bin/caddy")
            self.docker("commit", "--change", 'ENTRYPOINT ["caddy"]', target, self.proxy_tag)
            image = self.docker("image", "inspect", self.proxy_tag, "--format", "{{.Id}}")
            assert self.docker("run", "--rm", "--network", "none", "--entrypoint", "sha256sum", image, "/usr/bin/caddy").split()[0] == expected
            return image
        finally:
            self.docker("rm", "-v", "-f", *([target] if target else []), donor)

    def authorize(self) -> None:
        scopes = (READ_SCOPE, DEPLOY_SCOPE, OPERATE_SCOPE)
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
            "crash_budget": 1 if prior is None else 0,
        }
        captured = None
        proxy = None
        bindings = ()
        if prior is not None:
            captured = capture_prior_container(prior.container, self.image, expected_mounts=tuple((str(a), b, c) for a, b, c in prior.mounts), require_owner_lock=True, require_deployment_identity=True)
            self.ensure_proxy(prior)
            proxy = capture_prior_container(prior.proxy, self.proxy_image, expected_mounts=((str(prior.path("Caddyfile")), "/etc/caddy/Caddyfile", False),))
            bindings = required_exclusions(captured)
            config.update(fresh_guard_path="/management-initialization/fresh-guard.sock", fresh_prior_lock_path="/management-prior-owner.lock", archived_exclusions=[] if omit_archive else [asdict(item) for item in bindings])
        if self.worker_environment is not None:
            for channel in ("beta", "release"):
                runtime["environment"].update(
                    {
                        f"AUTO_CODER_{channel.upper()}_SUPERVISOR_INSTANCE_ID": channel + "-worker",
                        f"AUTO_CODER_{channel.upper()}_REPOSITORY": "fixture/" + channel,
                        f"AUTO_CODER_{channel.upper()}_BRIDGE_ADDRESS": self.worker_environment[f"SUPERVISOR_{channel.upper()}_IP"],
                        f"AUTO_CODER_{channel.upper()}_BRIDGE_PORT": "8843",
                        f"AUTO_CODER_{channel.upper()}_BRIDGE_SECRET_FILE": "/run/secrets/" + channel + "_worker_bridge",
                    }
                )
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
                    "clients": [{"client_id": "ancestor-client", "redirect_uris": [self.origin + "/callback"], "allowed_scopes": [READ_SCOPE, DEPLOY_SCOPE, OPERATE_SCOPE]}],
                    "enabled_scopes": [READ_SCOPE, DEPLOY_SCOPE, OPERATE_SCOPE],
                    "bind_host": "127.0.0.1",
                    "port": 8081,
                    "local_development": True,
                    **({"operating_policy_path": "/bootstrap/policy.yaml"} if self.worker_environment is not None else {}),
                }
            )
        )
        mounts = [(root / "state", "/management-child-state", True), (root / "journal", "/management-operations", True), (self.oauth, "/management-oauth", True), (root / "bootstrap", "/bootstrap", False)]
        if self.worker_environment is not None:
            (root / "bootstrap" / "policy.yaml").write_text(yaml.safe_dump({"profile": "full", "targets": {channel + "-worker": {"channel": channel, "repositories": ["fixture/" + channel]} for channel in ("beta", "release")}}))
            for channel in ("beta", "release"):
                mounts.append((self.root / "workers" / (channel + "-secret"), "/run/secrets/" + channel + "_worker_bridge", False))
        if prior is not None:
            mounts += [(root / "guard", "/management-initialization", False), (prior.path("state") / ".supervisor-owner.lock", "/management-prior-owner.lock", False), (prior.path("journal"), "/management-prior-operations", False)]
            if not omit_archive:
                for item in bindings:
                    mounts += [(Path(item.host_journal_root), item.journal_root, False), (Path(item.host_fence), item.fence, False)]
        self.configure_profile(name, root, config, runtime, management, mounts, fresh=prior is not None)
        config_file.write_text(json.dumps(config))
        container_name = "ancestor-fixture-" + uuid.uuid4().hex[:16]
        self.names.append(container_name)
        args = ["create", "--name", container_name, "--read-only", "--publish", f"127.0.0.1:{self.port}:8080"]
        # Docker does not program published ports through an internal-only
        # endpoint. Keep worker isolation, give only management a bridge ingress.
        args += ["--network", "bridge"]
        for source, target, writable in mounts:
            args += ["--mount", f"type=bind,src={source},dst={target}" + ("" if writable else ",readonly")]
        args += [self.image, "--config", "/bootstrap/config.json"]
        if prior is not None:
            args += ["--fresh-initialization"]
        container = self.docker(*args)
        if self.worker_environment is not None:
            self.docker("network", "connect", self.worker_network, container)
            connected = json.loads(self.docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container))
            assert set(connected) == {"bridge", self.worker_network}
        generation = Generation(name, root, container, mounts)
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
                str(config["runtimes"][0]["receipt"]).removeprefix("sha256:"),
                self.application_digest,
                str(config["initial_runtime"]),
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

    def configure_profile(self, name, root, config, runtime, management, mounts, *, fresh: bool) -> None:
        """Default source profile; installed fixtures override trusted preparation."""

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

    def stop(self, generation: Generation, *, stop_proxy: bool = True) -> None:
        self.docker("stop", "--time", "40", *([generation.proxy] if generation.proxy and stop_proxy else []), generation.container, timeout=50)
        self.checkpoint(generation.path("journal") / "management-operations.sqlite3")

    def ensure_proxy(self, generation: Generation) -> None:
        if generation.proxy is not None:
            return
        actual = json.loads(self.docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", generation.container))
        assert actual == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}]}, "actual loopback Caddy port mapping unavailable"
        caddyfile = generation.path("Caddyfile")
        caddyfile.write_text("http://127.0.0.1:8080 {\n reverse_proxy 127.0.0.1:8081 {\n header_up Host 127.0.0.1:8081\n }\n}\n")
        caddyfile.chmod(0o600)
        name = "ancestor-proxy-" + uuid.uuid4().hex[:16]
        self.names.append(name)
        generation.proxy = self.docker(
            "run", "-d", "--name", name, "--network", "container:" + generation.container, "--mount", f"type=bind,src={caddyfile},dst=/etc/caddy/Caddyfile,readonly", "--entrypoint", "caddy", self.proxy_image, "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"
        )

    def call(self, generation: Generation, tool: str, arguments: dict[str, object], *, authenticated: bool = True):
        assert generation.proxy is not None
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Host": "127.0.0.1:8080"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", data=json.dumps(request).encode(), headers=headers), timeout=5) as response:
                return json.loads(response.read())["result"]["structuredContent"]
        except urllib.error.HTTPError as error:
            if not authenticated:
                return {"http_status": error.code}
            raise ValueError("synthetic authenticated proxy HTTP unavailable") from None

    def ready(self, generation: Generation, *, fresh: bool) -> None:
        self.ensure_proxy(generation)
        deadline = time.monotonic() + 60
        last = "not observed"
        while time.monotonic() < deadline:
            try:
                observation = ""
                if fresh:
                    code = "import json;from pathlib import Path;from auto_coder.management.fresh_initialization import read_fresh_status;print(json.dumps(read_fresh_status(Path('/management-child-state'))))"
                    status = json.loads(self.docker("exec", generation.container, "python", "-c", code, timeout=10).splitlines()[-1])
                    last = "fresh initialization status=" + str(status["status"])
                    if status["status"] not in {"launched", "succeeded"}:
                        time.sleep(0.1)
                        continue
                    observation = status["observation_id"]
                last = "unauthenticated Caddy challenge"
                if self.call(generation, "auto_coder_whoami", {}, authenticated=False)["http_status"] != 401:
                    time.sleep(0.1)
                    continue
                last = "authenticated Caddy whoami"
                result = self.call(generation, "auto_coder_whoami", {"observation_id": observation})
                if result["uid"] == "operator" and (not fresh or status["status"] == "succeeded"):
                    if self.worker_baseline is not None:
                        last = "authenticated worker identity readback"
                        self.assert_worker_reads(generation)
                    return
            except (subprocess.CalledProcessError, ValueError, KeyError, OSError, http.client.HTTPException) as error:
                # Transport startup failures may retry, never count as proof.
                # Keep only stage and exception type, not headers/credential data.
                last = last.split(";", 1)[0] + "; " + transport_failure(error)
            time.sleep(0.1)
        states = self.docker("inspect", "--format", "{{json .State.Status}} {{json .Config.Entrypoint}} {{json .Config.Cmd}}", generation.container, generation.proxy)
        raise AssertionError("exact synthetic child readiness was not proven; " + last + "; " + states)

    @staticmethod
    def child(generation: Generation):
        with sqlite3.connect((generation.path("state") / "management-child.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
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

    def start_workers(self) -> None:
        root = self.root / "workers"
        for name in ("beta/home", "release/home", "beta-workspace", "release-workspace"):
            (root / name).mkdir(parents=True)
        for channel in ("beta", "release"):
            secret = root / (channel + "-secret")
            secret.write_bytes(base64.b64encode(os.urandom(48)) + b"\n")
            secret.chmod(0o600)
        fixtures = Path(__file__).parent / "fixtures"
        octet = random.SystemRandom().randrange(64, 239)
        self.worker_environment = dict(
            os.environ,
            AUTOCODER_SUPERVISOR_IMAGE=self.image,
            SUPERVISOR_RUN_ROOT=str(root),
            SUPERVISOR_FIXTURES_DIR=str(fixtures.resolve()),
            SUPERVISOR_HOST_UID=str(os.getuid()),
            SUPERVISOR_HOST_GID=str(os.getgid()),
            SUPERVISOR_BRIDGE_SUBNET=f"10.252.{octet}.0/24",
            SUPERVISOR_RELEASE_IP=f"10.252.{octet}.11",
            SUPERVISOR_BETA_IP=f"10.252.{octet}.12",
            SUPERVISOR_TEST_REQUEST_AUDIT="1",
        )
        # Internal networks need an explicit host bridge address for the real
        # published Caddy endpoint and the installed fixture's HTTP authority.
        # Reuse the canonical worker fixture; only its disposable IPAM changes.
        private_compose = worker_compose_profile(fixtures, f"10.252.{octet}.0/24", f"10.252.{octet}.1")
        compose_path = root / "compose.yml"
        compose_path.write_text(yaml.safe_dump(private_compose))
        self.worker_compose = ["docker", "compose", "--project-name", "ancestor-workers-" + uuid.uuid4().hex[:12], "-f", str(compose_path)]
        self.compose("up", "-d", "--no-build")
        self.worker_manager = self.compose("ps", "-q", "manager")
        networks = json.loads(self.docker("inspect", self.worker_manager))[0]["NetworkSettings"]["Networks"]
        assert len(networks) == 1
        self.worker_network = next(iter(networks))
        deadline = time.monotonic() + 60
        while True:
            try:
                self.worker_baseline = self.workers()
                break
            except (subprocess.CalledProcessError, ValueError):
                assert time.monotonic() < deadline, "actual fixture worker bridge unavailable"
                time.sleep(0.1)
        # Positive control proves the receiver audit sees actual incoming POSTs.
        for channel in ("beta", "release"):
            code = (
                "import sys;from auto_coder.management.worker_network_client import WorkerNetworkClient,WorkerEndpointConfig;"
                "c=WorkerNetworkClient(WorkerEndpointConfig.environment(sys.argv[1]));c.inspect_supervisor();"
                "c._call('POST','/v1/fixture-audit-probe',{'operation_id':'fixture-audit-probe'});print('observed')"
            )
            self.docker("exec", self.worker_manager, "python", "-c", code, channel)
        assert self.worker_posts() == {"beta": 1, "release": 1}

    def compose(self, *args: str) -> str:
        return subprocess.check_output([*self.worker_compose, *args], env=self.worker_environment, stderr=subprocess.STDOUT, text=True, timeout=90).strip()

    def workers(self):
        code = (
            "import json,sys;from auto_coder.management.worker_network_client import WorkerNetworkClient,WorkerEndpointConfig;"
            "c=WorkerNetworkClient(WorkerEndpointConfig.environment(sys.argv[1]));i=c.inspect_supervisor();w=c.observe_worker();"
            "assert i.result_code=='OK' and w.result_code=='OK';o=w.data['observation'];"
            "print(json.dumps({'supervisor':i.supervisor_generation,'run':o['process_run_id'],'deployment':o['deployment_generation']}))"
        )
        result = {}
        for channel in ("beta", "release"):
            observed = json.loads(self.docker("exec", self.worker_manager, "python", "-c", code, channel))
            container = self.compose("ps", "-q", channel)
            metadata = json.loads(self.docker("inspect", container))[0]
            result[channel] = {**observed, "container": container, "started": metadata["State"]["StartedAt"]}
        return result

    def worker_posts(self):
        return {channel: len((self.root / "workers" / channel / "fixture-posts").read_text().splitlines()) for channel in ("beta", "release")}

    def seed_workers(self, generation: Generation):
        records = {}
        store = ManagementOperationStore(generation.path("journal"))
        for channel, observed in self.worker_baseline.items():
            intent = OperationIntent(
                ManagedTarget(channel + "-worker", channel, "fixture/" + channel), "reevaluate_target", (ActionParameter("target_id", channel + "-worker"), ActionParameter("target_type", "issue"), ActionParameter("number", 2603)), "old-worker-" + channel, observed["run"], observed["deployment"]
            )
            record = store.admit(intent, self.authority).record
            assert record is not None
            assert store.transition(record.operation_id, 0, OperationPhase.FAILED, evidence=EffectReceipt(record.operation_id, "synthetic-no-effect", 1, "no_effect_sent")).record is not None
            records[channel] = record
        self.checkpoint(generation.path("journal") / "management-operations.sqlite3")
        return records

    def assert_worker_reads(self, generation: Generation) -> None:
        for channel, expected in self.worker_baseline.items():
            result = self.call(generation, "auto_coder_deployment_status", {"target_id": channel + "-worker"})
            assert result["status"] == "OK" and result["alive"] is True
            assert result["process_state"] == "RUNNING" and result["supervisor_generation"] == expected["supervisor"]
            observed = result["worker_observation"]
            assert observed["process_run_id"] == expected["run"]
            assert observed["deployment_generation"] == expected["deployment"]
            assert result["worker"]["process_run_id"] == expected["run"]
            assert result["worker"]["deployment_generation"] == expected["deployment"]

    def assert_workers(self, generation: Generation, records) -> None:
        self.assert_worker_reads(generation)
        assert self.workers() == self.worker_baseline
        before = self.worker_posts()
        for channel, record in records.items():
            reply = self.call(
                generation,
                "auto_coder_reevaluate_target",
                {"target_id": channel + "-worker", "target_type": "issue", "number": 2603, "idempotency_key": record.intent.idempotency_key, "expected_process_run_id": record.intent.expected_incarnation, "expected_deployment_generation": record.intent.expected_artifact},
            )
            assert reply["status"] == "CONFLICT" and reply.get("operation_id") is None
        assert self.worker_posts() == before == {"beta": 1, "release": 1}
        assert self.workers() == self.worker_baseline

    def reconnect_proxy(self, generation: Generation) -> None:
        root = generation.path("watcher")
        root.mkdir(mode=0o700)
        config = root / "config.json"
        config.write_text(json.dumps({"state_root": str(root), "manager_id": generation.container, "manager_image": self.image, "proxy_id": generation.proxy, "proxy_image": self.proxy_image, "config_sha256": hashlib.sha256(generation.path("Caddyfile").read_bytes()).hexdigest()}))
        config.chmod(0o600)
        process = subprocess.Popen([sys.executable, "-m", "auto_coder.management.proxy_watch_cli", "--config", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)

    def save_diagnostics(self) -> None:
        destination = os.environ.get("SUPERVISOR_RESULTS_PATH")
        if not destination:
            return
        directory = Path(destination).parent / ("cutover-sanitized-" + uuid.uuid4().hex)
        document = {}
        if hasattr(self, "worker_network"):
            document["private_network"] = json.loads(self.docker("network", "inspect", "--format", "{{json .IPAM.Config}}", self.worker_network))
        for generation in self.generations:
            observations = {}
            try:
                child = self.child(generation)
                observations["child"] = {key: child.get(key) for key in ("phase", "child_generation", "selected_runtime", "exit_cause", "readiness", "intended_stop", "recovery_used")}
            except (OSError, sqlite3.Error, ValueError, TypeError):
                observations["child"] = "unavailable"
            for kind, container in (("management", generation.container), ("proxy", generation.proxy)):
                if container is None:
                    continue
                facts = json.loads(self.docker("inspect", "--format", "{{json .State}}", container))
                observations[kind] = {key: facts.get(key) for key in ("Status", "Running", "ExitCode")}
                observations[kind]["ports"] = json.loads(self.docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container))
                observations[kind]["network_mode"] = self.docker("inspect", "--format", "{{json .HostConfig.NetworkMode}}", container)
                try:
                    if facts.get("Pid"):
                        observations[kind]["network_namespace"] = os.readlink(f"/proc/{facts['Pid']}/ns/net")
                        listeners = []
                        for table in ("tcp", "tcp6"):
                            for line in Path(f"/proc/{facts['Pid']}/net/{table}").read_text().splitlines()[1:]:
                                fields = line.split()
                                if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) in (8080, 8081):
                                    listeners.append(fields[1])
                        observations[kind]["listeners"] = listeners
                except (OSError, ValueError, IndexError) as error:
                    observations[kind]["network_observation"] = type(error).__name__
                logs = self.docker("logs", "--tail", "60", container)
                observations[kind]["errors"] = diagnostic_log_lines(logs, getattr(self, "token", ""))
            document[generation.name] = observations
        publish_diagnostics(directory, document)
        print("cutover-sanitized-observations " + json.dumps(document)[:16000])

    def close(self) -> None:
        try:
            self.save_diagnostics()
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            # Diagnostics cannot replace the test outcome or prevent cleanup.
            print("cutover-sanitized-diagnostics unavailable: " + type(error).__name__)
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
        for fd in self.fds:
            os.close(fd)
        if self.names:
            subprocess.run(["docker", "rm", "-f", *self.names], capture_output=True, timeout=60)
        if self.worker_compose is not None:
            self.compose("down", "--volumes", "--remove-orphans")
        subprocess.run(["docker", "image", "rm", self.tag, self.proxy_tag], capture_output=True, timeout=30)


@pytest.fixture
def chain(tmp_path: Path, request):
    if os.geteuid() != 0:
        pytest.skip("host root is required for disposable namespace and proxy evidence")
    fixture = Chain.__new__(Chain)
    try:
        fixture.__init__(tmp_path, with_workers=request.node.callspec.params["boundary"] == "complete")
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
    worker_records = chain.seed_workers(a) if chain.worker_baseline is not None else None
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
        chain.assert_workers(c, worker_records)
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
            proxy_before = json.loads(chain.docker("inspect", c.proxy))[0]
            chain.stop(c, stop_proxy=False)
            chain.docker("start", c.container)
            chain.reconnect_proxy(c)
            chain.ready(c, fresh=True)
            proxy_after = json.loads(chain.docker("inspect", c.proxy))[0]
            manager_after = json.loads(chain.docker("inspect", c.container))[0]
            assert proxy_after["State"]["StartedAt"] != proxy_before["State"]["StartedAt"]
            assert os.readlink(f"/proc/{proxy_after['State']['Pid']}/ns/net") == os.readlink(f"/proc/{manager_after['State']['Pid']}/ns/net")
            until = time.monotonic() + 30
            while not (c.path("watcher") / "proxy-reconnect.json").exists() or json.loads((c.path("watcher") / "proxy-reconnect.json").read_text())["phase"] != "done":
                assert time.monotonic() < until, "proxy reattachment was not durably recorded"
                time.sleep(0.1)
            chain.assert_workers(c, worker_records)
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
