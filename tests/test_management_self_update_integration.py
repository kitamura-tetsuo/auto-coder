"""Real installed management children, authenticated HTTP, and Supervisor recovery.

Only external Firebase/CI HTTP services are supplied by a disposable loopback
server. The installed child executes the production application, grant service,
policy, operation journal, staging verifier, and stable Supervisor unchanged.
A small fixture CLI wires those external service endpoints through construction
arguments; it does not replace management actions or authorization decisions.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.bootstrap_store import BootstrapStore
from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor
from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.self_update_handoff import HandoffPhase, HandoffStore
from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementReadyReceipt, ManagementRuntimeConfig, ManagementVersionStager
from auto_coder.tested_source_refs import REQUIRED_JOBS, SCHEMA_VERSION, TEST_PROFILE, WORKFLOW_PATH
from auto_coder.tested_source_refs import TestedSourcePublisher as SourcePublisher
from auto_coder.tested_source_refs import TestedSourceRecord as SourceRecord
from auto_coder.tested_source_refs import serialize_record, tag_name
from auto_coder.util.gh_cache import GitHubGitDataClient

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]

REPOSITORY = "kitamura-tetsuo/auto-coder"
INSTANCE = "self-update-integration"
PROJECT = "self-update-production-boundary"
SCOPES = (READ_SCOPE, DEPLOY_SCOPE)
ROOT = Path(__file__).resolve().parents[1]

# This source is installed and hashed with every fixture revision. SDK endpoint
# configuration selects an actual local HTTP server, never a fake grant result.
_CHILD_ENTRYPOINT = """from __future__ import annotations
import asyncio
import json
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import uvicorn
from firebase_admin import auth, credentials, initialize_app
from google.auth.credentials import AnonymousCredentials
from mcp.types import ToolAnnotations
from auto_coder.logger_config import setup_logger
from auto_coder.management.action_scopes import READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
from auto_coder.management.mcp_resource import ManagementTool
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.util.gh_cache import GitHubGitDataClient

class LocalCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()

def main():
    args = sys.argv[1:]
    if args in (["--help"], ["management", "--help"], ["management", "serve", "--help"]):
        print("management serve --config PATH")
        return
    if len(args) != 4 or args[:3] != ["management", "serve", "--config"]:
        raise SystemExit(2)
    settings = load_management_settings(Path(args[3]))
    external = json.loads((settings.state_root / "external-services.json").read_text())
    identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / "allowed_uids.json"))
    app = initialize_app(LocalCredential(), {"projectId": settings.oauth.firebase_project_id}, name="self-update-boundary")
    identity._app = app
    sdk = auth._get_client(app=app)
    sdk._token_verifier.id_token_verifier.cert_url = external["url"] + "/certificates"
    sdk._user_manager.base_url = external["url"] + "/projects/" + settings.oauth.firebase_project_id
    grants = OAuthGrantService(settings.oauth, identity)
    source = TestedSourcePublisher(GitHubGitDataClient("", external["repository"], api_url=external["url"]), external["repository"])
    async def hold_request() -> dict[str, object]:
        entered = settings.state_root / "request-entered"
        release = settings.state_root / "release-request"
        entered.write_text("active")
        while not release.exists():
            await asyncio.sleep(0.02)
        (settings.state_root / "request-exited").write_text("drained")
        return {"drained": True}
    held = ManagementTool("fixture_hold_request", hold_request, (READ_SCOPE,), ToolAnnotations(readOnlyHint=True))
    application = create_management_app(settings, grants, (held,), self_update_source_authority=source)
    setup_logger(stream=sys.stdout, log_file=str(settings.state_root / "logs" / "fixture-child.log"))
    uvicorn.run(application, host=settings.bind_host, port=settings.port, access_log=False)

if __name__ == "__main__":
    main()
"""


def _run(arguments: list[str], cwd: Path | None = None, *, env: dict[str, str] | None = None, timeout: float = 90) -> str:
    completed = subprocess.run(arguments, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    assert completed.returncode == 0, f"{arguments!r}\n{completed.stdout}\n{completed.stderr}"
    return completed.stdout.strip()


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait(predicate, *, timeout: float = 20, description: str = "condition", interval: float = 0.03):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {description}")


@dataclass
class ExternalServices:
    repository: Path
    certificate: str
    records: dict[int, SourceRecord] = field(default_factory=dict)
    job_results: dict[int, dict[str, bool]] = field(default_factory=dict)
    disabled_users: set[str] = field(default_factory=set)
    unavailable: bool = False
    held_ci: threading.Event = field(default_factory=threading.Event)
    release_ci: threading.Event = field(default_factory=threading.Event)
    hold_next_ci: bool = False
    requests: list[str] = field(default_factory=list)

    def handler(self):
        services = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def respond(self, code: int, body: object) -> None:
                encoded = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                services.requests.append(self.path)
                if self.path == "/certificates":
                    self.respond(200, {"integration-key": services.certificate})
                    return
                if services.unavailable:
                    self.respond(503, {"error": "local evidence unavailable"})
                    return
                path = self.path.removeprefix(f"/repos/{REPOSITORY}/")
                if path.startswith("actions/runs/"):
                    if services.hold_next_ci:
                        services.hold_next_ci = False
                        services.held_ci.set()
                        services.release_ci.wait(25)
                    run_id = int(path.split("/")[2])
                    record = services.records.get(run_id)
                    if record is None:
                        self.respond(404, {})
                        return
                    if "/jobs?" in path:
                        outcomes = services.job_results[run_id]
                        jobs = [{"name": name, "status": "completed", "conclusion": "success" if outcomes[name] else "failure", "run_attempt": 1} for name in sorted(REQUIRED_JOBS)]
                        self.respond(200, {"total_count": len(jobs), "jobs": jobs})
                    else:
                        self.respond(200, {"id": run_id, "run_attempt": 1, "workflow_id": record.workflow_id, "head_sha": record.source_sha, "path": WORKFLOW_PATH, "status": "completed", "conclusion": "success" if all(services.job_results[run_id].values()) else "failure"})
                    return
                if path.startswith("git/ref/tags/"):
                    reference = "refs/tags/" + path.removeprefix("git/ref/tags/")
                    result = subprocess.run(["git", "rev-parse", "--verify", reference], cwd=services.repository, capture_output=True, text=True)
                    if result.returncode:
                        self.respond(404, {})
                    else:
                        self.respond(200, {"ref": reference, "object": {"type": "tag", "sha": result.stdout.strip()}})
                    return
                if path.startswith("git/tags/"):
                    oid = path.removeprefix("git/tags/")
                    raw = _run(["git", "cat-file", "tag", oid], services.repository)
                    header, message = raw.split("\n\n", 1)
                    fields = dict(line.split(" ", 1) for line in header.splitlines())
                    self.respond(200, {"tag": fields["tag"], "message": message, "object": {"type": fields["type"], "sha": fields["object"]}})
                    return
                self.respond(404, {})

            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                if self.path.endswith("/accounts:lookup"):
                    uid = payload.get("localId", [""])[0]
                    self.respond(200, {"users": [{"localId": uid, "disabled": uid in services.disabled_users, "validSince": "0"}]})
                    return
                self.respond(405, {})

        return Handler


def _snapshot_source(root: Path, revision: str) -> str:
    """Commit actual working production modules into an isolated tested source."""
    source = root / "src" / "auto_coder"
    if not source.exists():
        shutil.copytree(ROOT / "src" / "auto_coder", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build_provenance.json"))
        (source / "cli.py").write_text(_CHILD_ENTRYPOINT, encoding="utf-8")
        dependencies = ["requests", "pydantic", "pydantic-settings", "click", "python-dotenv", "GitPython", "pyyaml", "loguru", "httpx", "hishel", "ghapi", "fastcore", "PyGithub", "PyJWT", "PyNaCl", "fastapi", "uvicorn", "firebase-admin", "mcp", "pytest", "tomli-w"]
        requirements = [f"{name}=={importlib.metadata.version(name)}" for name in dependencies]
        (root / "pyproject.toml").write_text(
            "[build-system]\nrequires=['setuptools>=68','wheel']\nbuild-backend='setuptools.build_meta'\n"
            "[project]\nname='auto-coder'\nversion='1.2.3'\nrequires-python='>=3.12'\n"
            f"dependencies={json.dumps(requirements)}\n"
            "[project.scripts]\nauto-coder='auto_coder.cli:main'\n"
            "[tool.setuptools.packages.find]\nwhere=['src']\n"
            "[tool.setuptools.package-dir]\n''='src'\n"
            "[tool.setuptools.package-data]\nauto_coder=['*.json','*.yaml','management/static/*.html','management/static/*.js']\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n*.egg-info/\n.venv/\n", encoding="utf-8")
        tests = root / "tests"
        tests.mkdir()
        shutil.copy2(ROOT / "tests" / "test_management_operation_store.py", tests)
        _run(["git", "init", "-q"], root)
        _run(["git", "config", "user.email", "self-update@example.invalid"], root)
        _run(["git", "config", "user.name", "Management integration"], root)
        uv = shutil.which("uv") or pytest.fail("uv is required for real installed runtime tests")
        # CI locks the checked-out project. This new disposable project first
        # needs its own lock; installed staging still uses production --frozen.
        lock_environment = {name: value for name, value in os.environ.items() if name != "UV_LOCKED"}
        _run([uv, "lock", "--python", sys.executable], root, env=lock_environment)
    (source / "fixture_revision.json").write_text(json.dumps({"revision": revision}), encoding="utf-8")
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-qm", revision], root)
    return _run(["git", "rev-parse", "HEAD"], root)


def _test_source(services: ExternalServices, sha: str, run_id: int) -> SourceRecord:
    """Ground local CI receipts in selected checks of the exact source commit.

    Required job labels exercise the production publisher's original-run
    binding. This disposable service does not claim to run the complete GitHub
    source workflow or Chromium; its evidence is the actual checks below.
    """
    env = {**os.environ, "PYTHONPATH": str(services.repository / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    quality = subprocess.run([sys.executable, "-m", "compileall", "-q", "src/auto_coder/management"], cwd=services.repository, env=env, capture_output=True, text=True, timeout=60)
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_management_operation_store.py"], cwd=services.repository, env=env, capture_output=True, text=True, timeout=60)
    outcomes = {"Source identity and quality": quality.returncode == 0, "Complete source tests with Chromium": tests.returncode == 0}
    assert all(outcomes.values()), quality.stderr + tests.stdout + tests.stderr
    assert _run(["git", "rev-parse", "HEAD"], services.repository) == sha
    lock = _run(["git", "show", f"{sha}:uv.lock"], services.repository)
    record = SourceRecord(SCHEMA_VERSION, REPOSITORY, sha, "1.2.3", hashlib.sha256((lock + "\n").encode()).hexdigest(), "3.12", run_id, 1, 42, WORKFLOW_PATH, TEST_PROFILE, "success")
    services.records[run_id] = record
    services.job_results[run_id] = outcomes
    _run(["git", "tag", "-a", tag_name(sha), "-m", serialize_record(record), sha], services.repository)
    return record


@dataclass
class ManagementSystem:
    root: Path
    source: Path
    current_sha: str
    candidate_sha: str
    config_path: Path
    bootstrap_path: Path
    policy: Path
    origin: str
    services: ExternalServices
    server: ThreadingHTTPServer
    key: rsa.RSAPrivateKey
    publisher: SourcePublisher
    stager: ManagementVersionStager
    current: ManagementReadyReceipt
    candidate: ManagementReadyReceipt
    supervisor: subprocess.Popen[bytes] | None = None
    supervisor_log: object = None

    @property
    def settings(self):
        return load_management_settings(self.config_path)

    @property
    def bootstrap(self):
        return BootstrapConfig.read(self.bootstrap_path)

    @property
    def journal(self):
        return ManagementOperationStore(self.root / "operations")

    @property
    def child(self):
        return BootstrapStore(self.root / "supervisor", INSTANCE).load()

    def signed(self, uid: str = "operator") -> str:
        now = int(time.time())
        return jwt.encode({"iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT, "sub": uid, "iat": now - 10, "exp": now + 600, "auth_time": now - 20, "firebase": {"sign_in_provider": "google.com"}}, self.key, algorithm="RS256", headers={"kid": "integration-key"})

    def connect(self) -> httpx.Client:
        return httpx.Client(base_url=self.origin, timeout=120, trust_env=False)

    def issue(self, *, scopes: tuple[str, ...] = SCOPES, uid: str = "operator") -> str:
        with self.connect() as client:
            verifier = "v" * 48
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            redirect_uri = self.origin + f"/{uid}-callback"
            page = client.get(
                "/authorize", params={"response_type": "code", "client_id": uid + "-client", "redirect_uri": redirect_uri, "resource": self.origin + "/mcp", "scope": " ".join(scopes), "state": "integration-state", "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=True
            )
            assert page.status_code == 200, page.text
            data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
            approved = client.post(f"/authorize/{data['requestId']}/allow", data={"csrf": data["csrf"], "firebase_id_token": self.signed(uid)}, headers={"Origin": self.origin})
            assert approved.status_code == 303, approved.text
            code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
            tokens = client.post("/token", data={"grant_type": "authorization_code", "client_id": uid + "-client", "code": code, "redirect_uri": redirect_uri, "resource": self.origin + "/mcp", "code_verifier": verifier})
            assert tokens.status_code == 200, tokens.text
            return tokens.json()["access_token"]

    def request(self, bearer: str | None, name: str, arguments: dict[str, object] | None = None) -> httpx.Response:
        with self.connect() as client:
            headers = {"Accept": "application/json, text/event-stream"}
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})

    def call(self, bearer: str, name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        response = self.request(bearer, name, arguments)
        assert response.status_code == 200, response.text
        payload = response.json()["result"]
        assert payload.get("isError") is not True, payload
        result = payload["structuredContent"]
        assert isinstance(result, dict), result
        return result

    def start(self, *, wait: bool = True) -> None:
        assert self.supervisor is None or self.supervisor.poll() is not None
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.supervisor_log = (self.root / "supervisor.log").open("ab")
        self.supervisor = subprocess.Popen([sys.executable, "-m", "auto_coder.management.bootstrap_supervisor", "--config", str(self.bootstrap_path)], cwd=self.root, env=environment, stdout=self.supervisor_log, stderr=subprocess.STDOUT)
        if wait:
            self.wait_serving()

    def wait_serving(self, *, runtime: str | None = None, timeout: float = 120) -> None:
        def observed():
            if runtime and self.child.selected_runtime != runtime:
                return False
            try:
                return self.request(None, "auto_coder_whoami").status_code == 401
            except (httpx.TransportError, OSError):
                return False

        try:
            _wait(observed, timeout=timeout, description="real protected management HTTP child")
        except AssertionError:
            raise AssertionError((self.root / "supervisor.log").read_text()) from None

    def close(self) -> None:
        self.services.release_ci.set()
        (self.root / "oauth" / "release-request").touch()
        if self.supervisor is not None and self.supervisor.poll() is None:
            self.supervisor.terminate()
            self.supervisor.wait(timeout=10)
        try:
            child = self.child
            if child.pid and ManagementBootstrapSupervisor._start_token(child.pid) == child.start_token:
                os.kill(child.pid, signal.SIGTERM)
                _wait(lambda: not ManagementBootstrapSupervisor._start_token(child.pid), timeout=12, description="fixture child exit")
        finally:
            if self.supervisor_log is not None:
                self.supervisor_log.close()
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def management_system(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None):
    root = tmp_path / "management-system"
    root.mkdir()
    source = root / "repository"
    source.mkdir()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "management-self-update")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    services = ExternalServices(source, certificate)
    server = ThreadingHTTPServer(("127.0.0.1", 0), services.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    external_url = f"http://127.0.0.1:{server.server_port}"
    publisher = SourcePublisher(GitHubGitDataClient("", REPOSITORY, api_url=external_url), REPOSITORY)
    origin = f"http://127.0.0.1:{_port()}"
    state = root / "oauth"
    state.mkdir()
    (state / "external-services.json").write_text(json.dumps({"url": external_url, "repository": REPOSITORY}), encoding="utf-8")
    (state / "allowed_uids.json").write_text(json.dumps({"project_id": PROJECT, "allowed_uids": ["operator", "other"]}), encoding="utf-8")
    policy = root / "policy.yaml"
    policy.write_text(yaml.safe_dump({"profile": "full", "targets": {"beta-worker": {"channel": "beta", "repositories": [REPOSITORY]}}, "management_self_update": {"enabled": True, "instance_id": INSTANCE, "repository": REPOSITORY}}), encoding="utf-8")
    uv = Path(shutil.which("uv") or pytest.fail("uv required"))
    uv_version = _run([str(uv), "--version"]).removeprefix("uv ")
    config_path = root / "management.yaml"
    bootstrap_path = root / "bootstrap.json"
    config_path.write_text(
        yaml.safe_dump(
            {
                "state_root": str(state),
                "public_origin": origin,
                "firebase_project_id": PROJECT,
                "clients": [{"client_id": uid + "-client", "redirect_uris": [origin + f"/{uid}-callback"], "allowed_scopes": list(SCOPES)} for uid in ("operator", "other")],
                "enabled_scopes": list(SCOPES),
                "bind_host": "127.0.0.1",
                "port": urlsplit(origin).port,
                "local_development": True,
                "operating_policy_path": str(policy),
                "operation_state_root": str(root / "operations"),
                "bootstrap_config_path": str(bootstrap_path),
                "self_update_source_repository": str(source),
                "self_update_python_executable": sys.executable,
                "self_update_uv_executable": str(uv),
                "self_update_uv_version": uv_version,
            }
        ),
        encoding="utf-8",
    )
    current_sha = _snapshot_source(source, "current")
    _test_source(services, current_sha, 11)
    candidate_sha = _snapshot_source(source, "candidate")
    _test_source(services, candidate_sha, 12)
    stager = ManagementVersionStager(ManagementRuntimeConfig(INSTANCE, REPOSITORY, source, root / "versions", Path(sys.executable), uv, uv_version, management_config=config_path, source_authority=publisher, protected_roots=(state, root / "operations", root / "supervisor")))
    current = stager.prepare(ManagementPrepareIntent(current_sha, "current")).receipt
    candidate = stager.prepare(ManagementPrepareIntent(candidate_sha, "candidate")).receipt
    assert not list(Path(current.environment_path).rglob("*.pyc"))
    assert not list(Path(candidate.environment_path).rglob("*.pyc"))
    from auto_coder.management.self_update_handoff import runtime_for_receipt

    bootstrap = BootstrapConfig(INSTANCE, root / "supervisor", state, root / "operations", origin + "/mcp", current.management_runtime_id, (), graceful_timeout=0.4, management_version_root=root / "versions", management_config=config_path, verification_timeout=30)
    runtimes = [runtime_for_receipt(bootstrap, item) for item in (current, candidate)]
    value = asdict(bootstrap)
    value["runtimes"] = [{**asdict(item), "environment": dict(item.environment)} for item in runtimes]
    bootstrap_path.write_text(json.dumps(value, default=lambda value: str(value) if isinstance(value, Path) else value), encoding="utf-8")
    system = ManagementSystem(root, source, current_sha, candidate_sha, config_path, bootstrap_path, policy, origin, services, server, key, publisher, stager, current, candidate)
    try:
        system.start()
        yield system
    finally:
        system.close()


def _update(system: ManagementSystem, bearer: str, key: str = "restart-once", *, source: str | None = None) -> dict[str, object]:
    return system.call(bearer, "auto_coder_update_management_service", {"source_sha": source or system.candidate_sha, "idempotency_key": key})


def _operation(system: ManagementSystem, bearer: str, operation_id: str) -> dict[str, object]:
    return system.call(bearer, "auto_coder_operation_get", {"operation_id": operation_id})


def _wait_operation(system: ManagementSystem, bearer: str, operation_id: str, statuses: set[str], *, timeout: float = 120) -> dict[str, object]:
    def observed():
        result = _operation(system, bearer, operation_id)
        return result if result["status"] in statuses else None

    return _wait(observed, timeout=timeout, interval=0.25, description=f"operation {operation_id} status in {sorted(statuses)}")


@contextmanager
def _request_pool(system: ManagementSystem, workers: int):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            yield pool
        finally:
            system.services.release_ci.set()
            (system.root / "oauth" / "release-request").touch()


def _effect_counts(system: ManagementSystem) -> list[tuple[str, int]]:
    with sqlite3.connect(system.root / "supervisor" / "management-child.sqlite3") as connection:
        return connection.execute("SELECT action,count(*) FROM effects GROUP BY action ORDER BY action").fetchall()


def test_real_self_update_drains_reconnects_twice_and_replays_original_operation(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue()
    old = system.child
    assert old.selected_runtime == system.current.management_runtime_id
    assert system.request(None, "auto_coder_update_management_service", {"source_sha": system.candidate_sha, "idempotency_key": "no-auth"}).status_code == 401
    readonly = system.issue(scopes=(READ_SCOPE,))
    assert system.request(readonly, "auto_coder_update_management_service", {"source_sha": system.candidate_sha, "idempotency_key": "no-deploy"}).status_code == 403
    assert not system.journal.list_operations().records

    handoff_database = system.root / "supervisor" / "management-self-update.sqlite3"
    with sqlite3.connect(handoff_database) as database:
        database.execute("CREATE TRIGGER fail_stop_commit BEFORE UPDATE OF phase ON handoffs WHEN NEW.phase='stopping' BEGIN SELECT RAISE(ABORT, 'controlled stop-intent persistence failure'); END")
        database.execute("CREATE TRIGGER fail_start_commit BEFORE UPDATE OF phase ON handoffs WHEN NEW.phase='starting' BEGIN SELECT RAISE(ABORT, 'controlled start-intent persistence failure'); END")
    system.services.hold_next_ci = True
    with _request_pool(system, 3) as pool:
        held = pool.submit(system.call, token, "fixture_hold_request")
        _wait(lambda: (system.root / "oauth" / "request-entered").exists(), description="active authenticated request")
        request_body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "auto_coder_update_management_service", "arguments": {"source_sha": system.candidate_sha, "idempotency_key": "restart-once"}}}).encode()
        endpoint = urlsplit(system.origin)
        initiating = socket.create_connection(("127.0.0.1", endpoint.port), timeout=5)
        headers = f"POST /mcp HTTP/1.1\r\nHost: {endpoint.netloc}\r\nAuthorization: Bearer {token}\r\nAccept: application/json, text/event-stream\r\nContent-Type: application/json\r\nContent-Length: {len(request_body)}\r\nConnection: close\r\n\r\n".encode()
        initiating.sendall(headers + request_body)
        original = _wait(lambda: next(iter(system.journal.list_operations().records), None), timeout=30, description="durable self-update admission")
        assert system.services.held_ci.wait(20)
        # The original response is definitely lost before CI preflight can finish.
        initiating.close()
        concurrent_replay = pool.submit(_update, system, token)
        conflicting = pool.submit(_update, system, token, "concurrent-new-key")
        assert conflicting.result(timeout=10)["status"] == "BUSY"
        replay = concurrent_replay.result(timeout=10)
        assert replay["operation_id"] == original.operation_id
        assert replay["status"] == "ACCEPTED"
        pending = _operation(system, token, original.operation_id)
        assert pending["status"] == "ACCEPTED"
        assert pending["effect_state"] == "not_sent"
        assert len(system.journal.list_operations().records) == 1
        assert system.journal.get(original.operation_id).record.authority == original.authority
        system.services.release_ci.set()
        store = HandoffStore(system.root / "supervisor", INSTANCE)
        _wait(lambda: store.read(original.operation_id) is not None, timeout=90, description="committed Supervisor handoff")
        _wait(lambda: "reconciliation unavailable operation=" + original.operation_id in (system.root / "supervisor.log").read_text(), description="failed persisted stop claim")
        assert store.read(original.operation_id).phase is HandoffPhase.SUBMITTED
        assert not system.child.intended_stop
        assert system.child.pid == old.pid
        with sqlite3.connect(handoff_database) as database:
            database.execute("DROP TRIGGER fail_stop_commit")
        _wait(lambda: system.child.intended_stop, timeout=30, description="Supervisor graceful drain")
        assert system.child.pid == old.pid
        assert ManagementBootstrapSupervisor._start_token(old.pid) == old.start_token
        assert system.child.selected_runtime == system.current.management_runtime_id
        assert not (system.root / "oauth" / "request-exited").exists()
        assert system.supervisor is not None
        system.supervisor.terminate()
        system.supervisor.wait(timeout=10)
        system.supervisor_log.close()
        system.start(wait=False)
        assert ManagementBootstrapSupervisor._start_token(old.pid) == old.start_token
        (system.root / "oauth" / "release-request").touch()
        assert held.result(timeout=15) == {"drained": True}
        _wait(lambda: store.read(original.operation_id).phase is HandoffPhase.STOPPED, timeout=20, description="real stopped child before blocked candidate start")
        assert system.child.pid == 0
        assert not ManagementBootstrapSupervisor._start_token(old.pid)
        assert system.child.selected_runtime == system.current.management_runtime_id
        with sqlite3.connect(handoff_database) as database:
            database.execute("DROP TRIGGER fail_start_commit")

    system.wait_serving(runtime=system.candidate.management_runtime_id)
    assert system.child.pid != old.pid
    assert not ManagementBootstrapSupervisor._start_token(old.pid)
    handoff_database = system.root / "supervisor" / "management-self-update.sqlite3"
    with sqlite3.connect(handoff_database) as database:
        database.execute("CREATE TRIGGER reject_proof BEFORE UPDATE OF record_json ON handoffs WHEN json_extract(NEW.record_json, '$.proof') IS NOT NULL BEGIN SELECT RAISE(ABORT, 'controlled proof persistence failure'); END")
    first_reconnect = system.issue()
    assert system.call(first_reconnect, "auto_coder_whoami")["uid"] == "operator"
    unproved = _operation(system, first_reconnect, original.operation_id)
    assert unproved["status"] != "SUCCEEDED"
    assert system.journal.get(original.operation_id).record.phase.value != "succeeded"
    with sqlite3.connect(handoff_database) as database:
        database.execute("DROP TRIGGER reject_proof")
        database.execute("CREATE TRIGGER reject_success BEFORE UPDATE OF phase ON handoffs WHEN NEW.phase='succeeded' BEGIN SELECT RAISE(ABORT, 'controlled success-receipt persistence failure'); END")
    failure_log = "reconciliation unavailable operation=" + original.operation_id
    earlier_failures = (system.root / "supervisor.log").read_text().count(failure_log)
    fresh = system.issue()
    assert system.call(fresh, "auto_coder_whoami")["uid"] == "operator"
    _wait(lambda: (system.root / "supervisor.log").read_text().count(failure_log) > earlier_failures, description="failed durable authenticated success receipt")
    assert store.read(original.operation_id).proof is not None
    assert store.read(original.operation_id).phase is HandoffPhase.AWAITING_PROOF
    assert _operation(system, fresh, original.operation_id)["status"] != "SUCCEEDED"
    journal_path = system.root / "operations" / "management-operations.sqlite3"
    with sqlite3.connect(journal_path) as database:
        database.execute("CREATE TRIGGER reject_terminal BEFORE UPDATE OF phase ON operations WHEN NEW.phase='succeeded' BEGIN SELECT RAISE(ABORT, 'controlled journal settlement persistence failure'); END")
    with sqlite3.connect(handoff_database) as database:
        database.execute("DROP TRIGGER reject_success")

    def confirmed_without_terminal():
        response = _operation(system, fresh, original.operation_id)
        record = system.journal.get(original.operation_id).record
        return response if record.effect.state.value == "confirmed" else None

    unsettled = _wait(confirmed_without_terminal, timeout=20, interval=0.25, description="durable effect evidence before blocked terminal journal settlement")
    assert unsettled["status"] != "SUCCEEDED"
    assert system.journal.get(original.operation_id).record.phase.value != "succeeded"
    assert store.read(original.operation_id).phase is HandoffPhase.SUCCEEDED
    with sqlite3.connect(journal_path) as database:
        database.execute("DROP TRIGGER reject_terminal")

    def successful():
        observed = _operation(system, fresh, original.operation_id)
        return observed if observed["status"] == "SUCCEEDED" else None

    succeeded = _wait(successful, timeout=15, description="runtime-bound protected authenticated proof")
    assert succeeded["operation_id"] == original.operation_id
    assert succeeded["candidate_source_sha"] == system.candidate_sha
    verification = succeeded["verification"]
    assert verification["handoff_phase"] == "succeeded"
    assert verification["selected_process_running"] is True
    assert verification["local_startup_reconciled"] is True
    assert verification["protected_challenge_observed"] is True
    assert verification["authenticated_readback_observed"] is True
    assert verification["verification_deadline"] > 0
    counts = _effect_counts(system)
    current = system.child
    assert dict(counts)["REQUEST_GRACEFUL_STOP"] == 1

    # A second independent reconnect sees the same operation and no new effect.
    second_reconnect = system.issue()
    assert system.call(second_reconnect, "auto_coder_whoami")["uid"] == "operator"
    assert _operation(system, second_reconnect, original.operation_id)["status"] == "SUCCEEDED"
    replay = _update(system, second_reconnect)
    assert replay["operation_id"] == original.operation_id
    assert system.child == current
    assert _effect_counts(system) == counts
    recorded = system.journal.get(original.operation_id).record
    assert recorded is not None and recorded.authority == original.authority
    assert recorded.intent.target.instance_id == INSTANCE
    assert recorded.intent.target.channel == "management"
    assert recorded.intent.target.repository == REPOSITORY
    assert len(system.journal.list_operations().records) == 1


def test_real_scope_policy_source_and_installed_lock_refusals_have_no_process_effect(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue()
    initial = system.child
    initial_effects = _effect_counts(system)
    policy = yaml.safe_load(system.policy.read_text())
    policy["management_self_update"]["enabled"] = False
    system.policy.write_text(yaml.safe_dump(policy), encoding="utf-8")
    disabled = _update(system, token, "disabled")
    assert disabled["status"] == "DENIED"
    assert not system.journal.list_operations().records
    policy["management_self_update"]["enabled"] = True
    system.policy.write_text(yaml.safe_dump(policy), encoding="utf-8")
    journal_path = system.root / "operations" / "management-operations.sqlite3"
    with sqlite3.connect(journal_path) as database:
        database.execute("CREATE TRIGGER reject_admission BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT, 'controlled journal persistence failure'); END")
    rejected_admission = _update(system, token, "journal-write-fails")
    assert rejected_admission["status"] == "UNAVAILABLE"
    assert not system.journal.list_operations().records
    assert system.child == initial
    with sqlite3.connect(journal_path) as database:
        database.execute("DROP TRIGGER reject_admission")
    invalid = _update(system, token, "invalid-source", source="HEAD")
    assert invalid["status"] == "INVALID_REQUEST"
    assert not system.journal.list_operations().records
    for extra in ({"command": "echo unexpected"}, {"path": "/tmp/other"}, {"repository": "untrusted/repository"}, {"channel": "beta"}):
        response = system.request(token, "auto_coder_update_management_service", {"source_sha": system.candidate_sha, "idempotency_key": "extra", **extra})
        assert response.status_code in {400, 403, 422}, response.text
    system.services.unavailable = True
    refused = _update(system, token, "unavailable-ci")
    refused = _wait_operation(system, token, refused["operation_id"], {"UNAVAILABLE", "NOT_READY", "FAILED", "NEEDS_ATTENTION"})
    assert refused["status"] in {"UNAVAILABLE", "NOT_READY", "FAILED", "NEEDS_ATTENTION"}
    system.services.unavailable = False
    worker_ready = Path(system.candidate.environment_path).parent / "READY.json"
    original_ready = worker_ready.read_bytes()
    malformed = json.loads(original_ready)
    malformed["lock_sha256"] = "f" * 64
    worker_ready.write_text(json.dumps(malformed), encoding="utf-8")
    wrong_lock = _update(system, token, "wrong-installed-lock")
    wrong_lock = _wait_operation(system, token, wrong_lock["operation_id"], {"NOT_READY"})
    assert wrong_lock["status"] == "NOT_READY", wrong_lock
    assert wrong_lock["disposition"] == "NOT_STARTED"
    worker_ready.write_bytes(original_ready)
    original_run = system.services.records[12]
    system.services.records[12] = replace(original_run, source_sha=system.current_sha)
    wrong_test = _update(system, token, "wrong-tested-source")
    wrong_test = _wait_operation(system, token, wrong_test["operation_id"], {"NOT_READY", "UNAVAILABLE"})
    assert wrong_test["status"] in {"NOT_READY", "UNAVAILABLE"}, wrong_test
    assert wrong_test["disposition"] == "NOT_STARTED"
    system.services.records[12] = original_run
    assert system.child == initial
    assert _effect_counts(system) == initial_effects
    assert system.call(token, "auto_coder_whoami")["uid"] == "operator"


@pytest.mark.parametrize("change", ["revoke-grant", "disable-policy"])
def test_original_authority_is_rechecked_after_real_admission_before_stop(management_system: ManagementSystem, change: str) -> None:
    system = management_system
    token = system.issue()
    old = system.child
    effects = _effect_counts(system)
    system.services.hold_next_ci = True
    with _request_pool(system, 2) as pool:
        request = pool.submit(_update, system, token, "pending-authority")
        assert system.services.held_ci.wait(20), "source verification did not reach the actual HTTP boundary"
        original = _wait(lambda: next(iter(system.journal.list_operations().records), None), description="accepted original authority")
        if change == "revoke-grant":
            with system.connect() as client:
                response = client.post("/revoke", data={"client_id": "operator-client", "token": token})
                assert response.status_code == 200, response.text
        else:
            policy = yaml.safe_load(system.policy.read_text())
            policy["management_self_update"]["enabled"] = False
            system.policy.write_text(yaml.safe_dump(policy), encoding="utf-8")
        system.services.release_ci.set()
        result = request.result(timeout=90)
    assert result["operation_id"] == original.operation_id
    fresh = system.issue()
    observed = _wait_operation(system, fresh, original.operation_id, {"DENIED", "FAILED", "NEEDS_ATTENTION"})
    assert observed["status"] != "SUCCEEDED"
    assert system.child == old
    assert system.journal.get(original.operation_id).record.authority == original.authority
    assert _effect_counts(system) == effects


def test_real_candidate_without_matching_operator_proof_restores_previous_once(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue()
    handoff_database = system.root / "supervisor" / "management-self-update.sqlite3"
    with sqlite3.connect(handoff_database) as database:
        database.execute("CREATE TRIGGER fail_restore_commit BEFORE UPDATE OF phase ON handoffs WHEN NEW.phase='restoring' BEGIN SELECT RAISE(ABORT, 'controlled restore-intent persistence failure'); END")
        database.execute("CREATE TRIGGER fail_restore_start_commit BEFORE UPDATE OF phase ON handoffs WHEN NEW.phase='restore_starting' BEGIN SELECT RAISE(ABORT, 'controlled restore-start persistence failure'); END")
    original = _update(system, token, "no-proof")
    operation_id = original["operation_id"]
    system.wait_serving(runtime=system.candidate.management_runtime_id)
    candidate = system.child
    assert system.request(None, "auto_coder_whoami").status_code == 401
    other = system.issue(uid="other")
    assert system.call(other, "auto_coder_whoami")["uid"] == "other"
    inaccessible = _operation(system, other, operation_id)
    assert inaccessible["status"] == "NOT_FOUND"
    assert system.journal.get(operation_id).record.phase.value != "succeeded"
    store = HandoffStore(system.root / "supervisor", INSTANCE)
    _wait(lambda: "reconciliation unavailable operation=" + operation_id in (system.root / "supervisor.log").read_text(), timeout=40, description="failed durable restore authorization")
    assert store.read(operation_id).phase is HandoffPhase.AWAITING_PROOF
    assert system.child.pid == candidate.pid
    assert not system.child.intended_stop
    with sqlite3.connect(handoff_database) as database:
        database.execute("DROP TRIGGER fail_restore_commit")
    _wait(lambda: system.child.pid == 0, timeout=20, description="candidate drained before blocked previous-runtime launch")
    assert not ManagementBootstrapSupervisor._start_token(candidate.pid)
    assert system.child.selected_runtime == system.candidate.management_runtime_id
    assert store.read(operation_id).phase in {HandoffPhase.RESTORING, HandoffPhase.RESTORE_DRAINING}
    with sqlite3.connect(handoff_database) as database:
        database.execute("DROP TRIGGER fail_restore_start_commit")
    system.wait_serving(runtime=system.current.management_runtime_id)
    assert system.child.pid != candidate.pid
    assert system.child.recovery_used
    fresh = system.issue()
    restored = _operation(system, fresh, operation_id)
    assert restored["status"] == "PROCESS_RESTORED", restored
    original_record = system.journal.get(operation_id).record
    assert original_record is not None and original_record.authority.trusted_grant_id
    counts = _effect_counts(system)
    generation = system.child.child_generation
    assert dict(counts)["RESTORE_PREVIOUS"] == 1
    for _ in range(2):
        assert _operation(system, fresh, operation_id)["status"] == "PROCESS_RESTORED"
    assert _effect_counts(system) == counts
    assert system.child.child_generation == generation


def test_original_grant_revoked_during_real_drain_restores_without_candidate_start(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue()
    original_child = system.child
    with _request_pool(system, 1) as pool:
        held = pool.submit(system.call, token, "fixture_hold_request")
        _wait(lambda: (system.root / "oauth" / "request-entered").exists(), description="authenticated request before grant revocation")
        accepted = _update(system, token, "revoke-after-handoff")
        operation_id = accepted["operation_id"]
        _wait(lambda: system.child.intended_stop, timeout=90, description="real handoff drain before revocation")
        assert system.child.pid == original_child.pid
        assert not (system.root / "oauth" / "request-exited").exists()
        settings = system.settings
        identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / "allowed_uids.json"))
        # The listening child is draining, so the operator invokes the same
        # production revocation service directly over its retained OAuth store.
        OAuthGrantService(settings.oauth, identity).revoke("operator-client", token)
        (system.root / "oauth" / "release-request").touch()
        assert held.result(timeout=15) == {"drained": True}
    store = HandoffStore(system.root / "supervisor", INSTANCE)
    _wait(lambda: store.read(operation_id).phase is HandoffPhase.PROCESS_RESTORED, timeout=30, description="exact previous-runtime return after original grant revocation")
    system.wait_serving(runtime=system.current.management_runtime_id)
    assert system.child.pid != original_child.pid
    assert system.child.recovery_used
    assert system.request(token, "auto_coder_whoami").status_code == 401
    fresh = system.issue()
    result = _operation(system, fresh, operation_id)
    assert result["status"] == "PROCESS_RESTORED", result
    with sqlite3.connect(system.root / "supervisor" / "management-child.sqlite3") as database:
        candidate_effects = database.execute("SELECT count(*) FROM effects WHERE runtime_ref=?", (system.candidate.management_runtime_id,)).fetchone()[0]
    assert candidate_effects == 0
    assert dict(_effect_counts(system))["RESTORE_PREVIOUS"] == 1


@pytest.mark.parametrize("boundary", ["journal-effect", "supervisor-handoff"])
def test_real_persistence_failure_retains_original_child_and_busy_lane(management_system: ManagementSystem, boundary: str) -> None:
    system = management_system
    token = system.issue()
    old = system.child
    effects = _effect_counts(system)
    if boundary == "journal-effect":
        database_path = system.root / "operations" / "management-operations.sqlite3"
        trigger = "CREATE TRIGGER fail_boundary BEFORE UPDATE OF effect_json ON operations WHEN json_extract(NEW.effect_json, '$[0]') = 'sent_unknown' BEGIN SELECT RAISE(ABORT, 'controlled effect persistence failure'); END"
    else:
        database_path = system.root / "supervisor" / "management-self-update.sqlite3"
        trigger = "CREATE TRIGGER fail_boundary BEFORE INSERT ON handoffs BEGIN SELECT RAISE(ABORT, 'controlled handoff persistence failure'); END"
    with sqlite3.connect(database_path) as database:
        database.execute(trigger)
    response = _update(system, token, "persist-boundary")
    operation_id = response["operation_id"]
    response = _wait_operation(system, token, operation_id, {"UNKNOWN"})
    recorded = system.journal.get(operation_id).record
    assert recorded is not None
    assert system.child == old
    assert _effect_counts(system) == effects
    if boundary == "journal-effect":
        assert response["status"] == "UNKNOWN"
        assert response["disposition"] == "NOT_STARTED"
        assert recorded.effect.state.value == "not_sent"
    else:
        assert response["status"] == "UNKNOWN"
        assert recorded.effect.state.value == "sent_unknown"
    busy = _update(system, token, "another-intent")
    assert busy["status"] == "BUSY"
    replay = _update(system, token, "persist-boundary")
    assert replay["operation_id"] == operation_id
    assert replay["status"] != "SUCCEEDED"
    assert system.child == old
    assert _effect_counts(system) == effects
    assert len(system.journal.list_operations().records) == 1
    with sqlite3.connect(database_path) as database:
        database.execute("DROP TRIGGER fail_boundary")
    if boundary == "supervisor-handoff":
        # Removing the outage cannot prove whether an earlier handoff was sent.
        fresh = system.issue()
        assert _update(system, fresh, "persist-boundary")["status"] == "UNKNOWN"
        assert _operation(system, fresh, operation_id)["status"] == "UNKNOWN"
        assert system.journal.get(operation_id).record.authority == recorded.authority
        assert system.child == old
        assert _effect_counts(system) == effects
