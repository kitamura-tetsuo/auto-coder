"""Real isolated Docker NEW owner, child and retained-grant readiness oracle."""

from __future__ import annotations

import base64
import gc
import hashlib
import http.client
import json
import os
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from auto_coder.management.action_scopes import READ_SCOPE
from auto_coder.management.identity import AdmissionResult, AdmissionStatus, ReasonCode, VerifiedPrincipal
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import AuthorizationRequest, OAuthGrantService
from auto_coder.management.oauth_store import OAuthStore
from tests.test_management_new_instance_preflight import _environment, request_file

pytestmark = pytest.mark.supervisor_integration


def _run_host(*arguments: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.fresh_host", *arguments], env=_environment(), capture_output=True, text=True, timeout=timeout)


def _wait_for_new_outcome(root: Path, timeout: float = 40) -> dict[str, object]:
    """Wait for the complete fsynced outcome, not merely its new inode."""
    path = root / "state/new-first-outcome.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_bytes())
            if isinstance(value, dict) and value.get("status") in {"unknown", "succeeded"}:
                return value
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise AssertionError("NEW first outcome was not durably recorded")


def _required_original(path: Path) -> dict[str, object]:
    return json.loads(path.read_bytes())


def _prepare_real_new_source(request_file: Path, *, docker_network: str | None = None, host_port: int | None = None) -> tuple[str, dict[str, object]]:
    assert (docker_network is None) == (host_port is None)
    root = request_file.parent
    request = _required_original(request_file)
    application = root / "application" / "auto_coder"
    application.parent.mkdir(mode=0o700)
    shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "auto_coder", application, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    request["application_root"] = str(application)
    request["new_instance"] = "new-docker-" + uuid.uuid4().hex[:12]
    request["request_id"] = str(uuid.uuid4())
    request["runtime_reference"] = "new-management"
    request_file.write_text(json.dumps(request, sort_keys=True))

    selected_executable = Path(str(request["executable"]))
    counter = root / "state" / "physical-child-invocations"
    script = (
        "#!/opt/auto-coder/.venv/bin/python\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(application.parent)!r})\n"
        f"with open({str(counter)!r}, 'a', encoding='utf-8') as launch:\n"
        "    launch.write(str(os.getpid()) + '\\n')\n"
        "    launch.flush()\n"
        "    os.fsync(launch.fileno())\n"
        "from auto_coder.cli import main\n"
        "main()\n"
    )
    selected_executable.write_text(script)
    selected_executable.chmod(0o700)
    origin = f"http://127.0.0.1:{host_port or 18881}"
    settings = OAuthSettings(
        Path(str(request["oauth_root"])) / "oauth.sqlite3",
        origin,
        "new-docker-fixture",
        (OAuthClient("new-client", (origin + "/callback",), allowed_scopes=(READ_SCOPE,)),),
        enabled_scopes=(READ_SCOPE,),
    )
    OAuthStore(settings)

    principal = VerifiedPrincipal("new-docker-fixture", "new-operator", time.time() - 10)
    permitted = AdmissionResult(AdmissionStatus.PERMITTED, ReasonCode.PERMITTED, principal)

    class SyntheticLoginIdentity:
        def verify_login(self, _token):
            return permitted

        def recheck_identity(self, _principal):
            return permitted

        def check_operator_policy(self, _principal):
            return permitted

    grants = OAuthGrantService(settings, SyntheticLoginIdentity())
    verifier = "v" * 48
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    pending = grants.begin_authorization(AuthorizationRequest("new-client", origin + "/callback", settings.resource, challenge, (READ_SCOPE,)), "browser", "csrf")
    code = grants.approve_authorization(pending.request_id, "browser", "csrf", "fixture-login").code
    assert code is not None
    token = grants.exchange_code("new-client", code, origin + "/callback", settings.resource, verifier).access_token
    del grants
    gc.collect()
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    (Path(str(request["oauth_root"])) / "allowed_uids.json").write_text(json.dumps({"project_id": "new-docker-fixture", "allowed_uids": ["new-operator"]}))

    bootstrap = {
        "instance_id": request["new_instance"],
        "state_root": request["state_root"],
        "journal_root": request["operation_root"],
        "oauth_root": request["oauth_root"],
        "listener_url": "http://127.0.0.1:18881/mcp",
        "initial_runtime": request["runtime_reference"],
        "runtimes": [
            {
                "reference": request["runtime_reference"],
                "receipt": "sha256:" + hashlib.sha256(selected_executable.read_bytes()).hexdigest(),
                "executable": request["executable"],
                "arguments": ["management", "serve", "--config", request["management_config"]],
                "source_root": request["application_root"],
                "environment": {"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/dev/shm"},
            }
        ],
        "fresh_guard_path": request["guard_root"] + "/fresh-guard.sock",
        "new_instance_initialization": True,
        "crash_budget": 0,
    }
    if docker_network is not None:
        bootstrap["new_docker_network"] = docker_network
        bootstrap["new_docker_host_port"] = host_port
    Path(str(request["bootstrap_config"])).write_text(json.dumps(bootstrap))
    Path(str(request["management_config"])).write_text(
        yaml.safe_dump(
            {
                "state_root": request["oauth_root"],
                "operation_state_root": request["operation_root"],
                "bootstrap_config_path": request["bootstrap_config"],
                "public_origin": origin,
                "firebase_project_id": "new-docker-fixture",
                "clients": [{"client_id": "new-client", "redirect_uris": [origin + "/callback"], "allowed_scopes": [READ_SCOPE]}],
                "enabled_scopes": [READ_SCOPE],
                "bind_host": "0.0.0.0" if docker_network is not None else "127.0.0.1",
                "port": 18881,
                "local_development": True,
            }
        )
    )
    return token, request


def _create_real_new_container(root: Path, request_file: Path, request: dict[str, object], *, docker_network: str | None = None, host_port: int | None = None, published_ip: str = "127.0.0.1") -> tuple[str, str, str]:
    image_base = os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544")
    dockerfile = root / "Dockerfile"
    dockerfile.write_text(f'FROM {image_base}\nENTRYPOINT ["python3", "-B", "-m", "auto_coder.management.bootstrap_supervisor"]\n')
    tag = "auto-coder-new-first:" + uuid.uuid4().hex
    subprocess.run(["docker", "build", "--network", "none", "-t", tag, str(root)], check=True, capture_output=True, text=True, timeout=120)
    image_id = subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True).strip()
    name = "auto-coder-new-first-" + uuid.uuid4().hex[:14]
    command = [
        "docker",
        "create",
        "--name",
        name,
        "--network",
        docker_network or "none",
        "--restart",
        "no",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.geteuid()}:{os.getegid()}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONPATH=" + str(Path(str(request["application_root"])).parent),
        "--env",
        "HOME=/dev/shm",
    ]
    if host_port is not None:
        command.extend(["--publish", f"{published_ip}:{host_port}:18881"])
    for key in ("state_root", "operation_root", "guard_root", "oauth_root", "bootstrap_config", "management_config", "executable", "application_root"):
        path = str(request[key])
        command.extend(["--mount", "type=bind,src=" + path + ",dst=" + path + (",readonly" if key in {"bootstrap_config", "management_config", "executable", "application_root"} else "")])
    command.extend(["--mount", f"type=bind,src={request_file},dst={request_file},readonly"])
    command.extend([image_id, "--config", str(request["bootstrap_config"]), "--new-initialization", "--request-file", str(request_file)])
    container_id = subprocess.check_output(command, text=True, timeout=40).strip()
    return container_id, image_id, tag


def _authorized_readback(container_id: str, token: str, observation_id: str, *, expected_status: int = 200) -> subprocess.CompletedProcess[str]:
    code = (
        "import http.client,json,os;"
        f"obs={observation_id!r};"
        f"expected={expected_status!r};"
        "payload=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',"
        "'params':{'name':'auto_coder_whoami','arguments':{'observation_id':obs}}}).encode();"
        "c=http.client.HTTPConnection('127.0.0.1',18881,timeout=3);"
        "c.request('POST','/mcp',payload,headers={'Authorization':'Bearer '+os.environ['SYNTHETIC_TOKEN'],"
        "'Content-Type':'application/json','Accept':'application/json, text/event-stream'});"
        "r=c.getresponse();data=r.read(65537);"
        "assert r.status==expected,(r.status,data);"
        "assert expected!=200 or json.loads(data)['result']['structuredContent']['uid']=='new-operator'"
    )
    return subprocess.run(["docker", "exec", "--env", "SYNTHETIC_TOKEN=" + token, container_id, "python3", "-B", "-c", code], capture_output=True, text=True, timeout=20)


def _host_mcp_response(host_port: int, *, token: str | None = None, observation_id: str = "") -> tuple[int, str, bytes]:
    """Use the host network stack, not docker exec, for the MCP acceptance call."""
    connection = http.client.HTTPConnection("127.0.0.1", host_port, timeout=3)
    try:
        if token is None:
            connection.request("GET", "/mcp")
        else:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "auto_coder_whoami", "arguments": {"observation_id": observation_id}}}).encode()
            connection.request("POST", "/mcp", payload, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        response = connection.getresponse()
        return response.status, response.getheader("WWW-Authenticate", ""), response.read(65537)
    finally:
        connection.close()


def _reserve_test_host_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _new_isolated_bridge(name: str, *, masquerade: bool = False) -> None:
    subprocess.run(
        ["docker", "network", "create", "--driver", "bridge", "--opt", "com.docker.network.bridge.enable_ip_masquerade=" + str(masquerade).lower(), name],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_new_docker_loopback_mcp_reaches_original_authenticated_child_from_host(request_file: Path) -> None:
    """A public hostname never replaces the original host-loopback-only port."""
    root = request_file.parent
    network = "ac-new-" + uuid.uuid4().hex[:24]
    port = _reserve_test_host_port()
    token, request = _prepare_real_new_source(request_file, docker_network=network, host_port=port)
    prior_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    tag = ""
    container_id = ""
    _new_isolated_bridge(network)
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request, docker_network=network, host_port=port)
        result = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert result.returncode == 0, result.stderr
        created = json.loads(result.stdout)
        assert len(created["network_id"]) == 64
        assert created["published_host_port"] == port
        assert created["launch_authorized"] is False

        started = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=150)
        assert started.returncode == 0, (started.stderr, subprocess.run(["docker", "logs", container_id], capture_output=True, text=True).stdout)
        assert json.loads(started.stdout)["status"] == "first-child-observed-awaiting-authenticated-readback"
        identity = json.loads((root / "state/new-first-launched.json").read_bytes())["observation_id"]
        assert len((root / "state/physical-child-invocations").read_text().splitlines()) == 1
        replay = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=20)
        assert replay.returncode == 1

        deadline = time.monotonic() + 45
        last_error = ""
        while time.monotonic() < deadline:
            try:
                status, challenge, _ = _host_mcp_response(port)
                if status != 401 or "Bearer" not in challenge:
                    last_error = f"unauthorized response: {status} {challenge}"
                else:
                    auth_status, _, body = _host_mcp_response(port, token=token, observation_id=identity)
                    if auth_status == 200 and json.loads(body)["result"]["structuredContent"]["uid"] == "new-operator":
                        break
                    last_error = f"authorized status: {auth_status}"
            except (OSError, ValueError, KeyError, http.client.HTTPException) as error:
                last_error = str(error)
            time.sleep(0.25)
        else:
            raise AssertionError("host-loopback authenticated MCP was not usable: " + last_error)

        assert _wait_for_new_outcome(root)["status"] == "succeeded"
        assert (root / "oauth/oauth.sqlite3").read_bytes() == prior_oauth
        assert len((root / "state/physical-child-invocations").read_text().splitlines()) == 1
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], check=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("invalid", ["public-ip", "wrong-port", "masquerade"])
def test_new_docker_loopback_refuses_unselected_publication_without_claim(request_file: Path, invalid: str) -> None:
    network = "ac-new-" + uuid.uuid4().hex[:24]
    port = _reserve_test_host_port()
    _token, request = _prepare_real_new_source(request_file, docker_network=network, host_port=port)
    prior_oauth = (request_file.parent / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    tag = ""
    container_id = ""
    _new_isolated_bridge(network, masquerade=invalid == "masquerade")
    try:
        container_id, image_id, tag = _create_real_new_container(
            request_file.parent,
            request_file,
            request,
            docker_network=network,
            host_port=port - 1 if invalid == "wrong-port" else port,
            published_ip="0.0.0.0" if invalid == "public-ip" else "127.0.0.1",
        )
        observed = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert observed.returncode == 1 and observed.stdout == "", (observed.stdout, observed.stderr)
        refused = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=30)
        assert refused.returncode == 1 and refused.stdout == ""
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
        assert not (request_file.parent / "guard/new-docker-first-start.json").exists()
        assert not (request_file.parent / "state/new-first-launch-claimed.json").exists()
        assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == prior_oauth
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], check=True, capture_output=True, timeout=30)


def test_new_docker_loopback_refuses_live_prior_host_listener_without_first_claim(request_file: Path) -> None:
    """A bound host endpoint is an authoritative refusal, before Docker start."""
    root = request_file.parent
    network = "ac-new-" + uuid.uuid4().hex[:24]
    port = _reserve_test_host_port()
    _token, request = _prepare_real_new_source(request_file, docker_network=network, host_port=port)
    prior_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    tag = ""
    container_id = ""
    _new_isolated_bridge(network)
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request, docker_network=network, host_port=port)
        assert _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id).returncode == 0
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as existing_listener:
            existing_listener.bind(("127.0.0.1", port))
            existing_listener.listen(1)
            refused = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=30)
            assert refused.returncode == 1 and refused.stdout == ""
            assert "already occupied" in refused.stderr
        assert not (root / "guard/new-docker-first-start.json").exists()
        assert not (root / "state/physical-child-invocations").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        assert (root / "oauth/oauth.sqlite3").read_bytes() == prior_oauth
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], check=True, capture_output=True, timeout=30)


def test_real_different_os_uid_cannot_claim_original_new_docker_first_start(request_file: Path) -> None:
    """A real non-owner process must fail before claiming or starting the child."""
    root = request_file.parent
    _token, request = _prepare_real_new_source(request_file)
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    prior_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    original_origin = (root / "guard/new-instance-origin.json").read_bytes()
    tag = ""
    container_id = ""
    other_uid_image = "auto-coder-new-nonowner:" + uuid.uuid4().hex
    other_uid_container = "auto-coder-new-nonowner-" + uuid.uuid4().hex[:14]
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request)
        before = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert before.returncode == 0, before.stderr

        with tempfile.TemporaryDirectory(prefix="ac-new-different-uid-") as directory:
            build_root = Path(directory)
            shutil.copytree(root / "application/auto_coder", build_root / "auto_coder")
            base = os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544")
            (build_root / "Dockerfile").write_text(f"FROM {base}\nCOPY auto_coder /opt/new-uid-src/auto_coder\n")
            subprocess.run(["docker", "build", "--network", "none", "-t", other_uid_image, str(build_root)], check=True, capture_output=True, text=True, timeout=120)

        code = (
            "import os,runpy,sys;"
            "assert os.geteuid()==65534;"
            "sys.stderr.write('verified-nonowner-euid=65534\\n');"
            f"sys.argv=['fresh_host','start-new-container','--request-file',{str(request_file)!r},"
            f"'--container-id',{container_id!r},'--image-id',{image_id!r}];"
            "runpy.run_module('auto_coder.management.fresh_host',run_name='__main__')"
        )
        refused = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                other_uid_container,
                "--network",
                "none",
                "--restart",
                "no",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--env",
                "PYTHONPATH=/opt/new-uid-src",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "HOME=/tmp",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=16m",
                "--mount",
                f"type=bind,src={root},dst={root},readonly",
                "--entrypoint",
                "python3",
                other_uid_image,
                "-B",
                "-c",
                code,
            ],
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert refused.returncode == 1 and refused.stdout == "", (refused.returncode, refused.stdout, refused.stderr)
        assert "verified-nonowner-euid=65534" in refused.stderr
        assert "fresh host observation unavailable" in refused.stderr
        assert "Permission denied" in refused.stderr or "owned by effective UID" in refused.stderr
        assert not (root / "guard/new-docker-first-start.json").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        assert not (root / "state/physical-child-invocations").exists()
        assert (root / "guard/new-instance-origin.json").read_bytes() == original_origin
        assert (root / "oauth/oauth.sqlite3").read_bytes() == prior_oauth
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
        after = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert after.returncode == 0, after.stderr
    finally:
        subprocess.run(["docker", "rm", "--force", other_uid_container], capture_output=True, timeout=30, check=False)
        subprocess.run(["docker", "image", "rm", other_uid_image], capture_output=True, timeout=30, check=False)
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)


def test_new_docker_loopback_refuses_live_prior_network_listener_without_claim(request_file: Path) -> None:
    """A live process on the selected bridge violates sole network ownership."""
    root = request_file.parent
    network = "ac-new-" + uuid.uuid4().hex[:24]
    port = _reserve_test_host_port()
    _token, request = _prepare_real_new_source(request_file, docker_network=network, host_port=port)
    original_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    prior_origin = (root / "guard/new-instance-origin.json").read_bytes()
    tag = ""
    container_id = ""
    old_listener_id = ""
    _new_isolated_bridge(network)
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request, docker_network=network, host_port=port)
        before = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert before.returncode == 0, before.stderr
        old_listener_name = "auto-coder-new-old-listener-" + uuid.uuid4().hex[:14]
        old_listener_id = subprocess.check_output(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                old_listener_name,
                "--network",
                network,
                "--restart",
                "no",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--entrypoint",
                "python3",
                os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544"),
                "-B",
                "-c",
                "import socket,time;listener=socket.socket();listener.bind(('0.0.0.0',18881));listener.listen(4);time.sleep(240)",
            ],
            text=True,
            timeout=40,
        ).strip()
        assert len(old_listener_id) == 64

        listener_ready = False
        for _attempt in range(30):
            probe = subprocess.run(
                ["docker", "exec", old_listener_id, "python3", "-B", "-c", "import socket;socket.create_connection(('127.0.0.1',18881),timeout=2).close()"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                listener_ready = True
                break
            time.sleep(0.2)
        assert listener_ready, "the real prior network listener never bound"

        observed = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert observed.returncode == 1 and observed.stdout == ""
        assert "network scope differs" in observed.stderr
        refused = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=30)
        assert refused.returncode == 1 and refused.stdout == ""
        assert "network scope differs" in refused.stderr
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", old_listener_id], text=True).strip() == "true"
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
        assert not (root / "guard/new-docker-first-start.json").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        assert not (root / "state/physical-child-invocations").exists()
        assert (root / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        assert (root / "guard/new-instance-origin.json").read_bytes() == prior_origin
    finally:
        if old_listener_id:
            subprocess.run(["docker", "rm", "--force", old_listener_id], check=True, capture_output=True, timeout=30)
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], check=True, capture_output=True, timeout=30)


def _first_launch_with_actual_admission_contention(root: Path, request_file: Path, container_id: str, image_id: str) -> dict[str, object]:
    """Prove independent hosts overlap at the real first-claim kernel lock."""
    first = subprocess.Popen(
        [sys.executable, "-B", "-c", _PAUSE_AFTER_HOST_CLAIM, str(request_file), container_id, image_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(),
    )
    try:
        assert first.stdout is not None
        assert select.select([first.stdout], [], [], 20)[0], "first host did not reach the claim barrier"
        assert first.stdout.readline() == "new-host-claim-fsynced\n"
        assert (root / "guard/new-docker-first-start.json").is_file()
        assert not (root / "state/physical-child-invocations").exists()
        competing = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=20)
        assert competing.returncode == 1 and "Resource temporarily unavailable" in competing.stderr, competing.stderr
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
        assert not (root / "state/new-first-launch-claimed.json").exists()
        os.kill(first.pid, signal.SIGCONT)
        stdout, stderr = first.communicate(timeout=140)
        assert first.returncode == 0, stderr[-2500:]
        observed = json.loads(stdout)
        assert isinstance(observed, dict)
        return observed
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=10)


@pytest.mark.parametrize("actual_admission_contention", [False, True])
def test_one_genuinely_new_docker_launch_and_retained_grant_exact_child_readback(request_file: Path, actual_admission_contention: bool) -> None:
    root = request_file.parent
    token, request = _prepare_real_new_source(request_file)
    prior_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    tag = ""
    container_id = ""
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request)
        preflight = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert preflight.returncode == 0, preflight.stderr
        assert json.loads(preflight.stdout)["launch_authorized"] is False
        if actual_admission_contention:
            observed = _first_launch_with_actual_admission_contention(root, request_file, container_id, image_id)
        else:
            started = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=150)
            assert started.returncode == 0, (started.stderr, subprocess.run(["docker", "logs", container_id], capture_output=True, text=True).stdout)
            observed = json.loads(started.stdout)
        assert observed["status"] == "first-child-observed-awaiting-authenticated-readback"
        assert observed["launch_authorized"] is False
        counter = root / "state/physical-child-invocations"
        assert len(counter.read_text().splitlines()) == 1
        assert (root / "state/new-first-launch-claimed.json").is_file()
        assert (root / "state/new-first-launched.json").is_file()
        observation_id = json.loads((root / "state/new-first-launched.json").read_bytes())["observation_id"]
        replay = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=20)
        assert replay.returncode == 1
        assert len(counter.read_text().splitlines()) == 1

        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            call = _authorized_readback(container_id, token, observation_id)
            if call.returncode == 0:
                break
            outcome_path = root / "state/new-first-outcome.json"
            try:
                early_outcome = json.loads(outcome_path.read_bytes())
            except (FileNotFoundError, json.JSONDecodeError):
                early_outcome = {}
            if early_outcome.get("status") == "unknown":
                logs = subprocess.run(["docker", "logs", "--tail", "40", container_id], capture_output=True, text=True, timeout=10)
                raise AssertionError("real child exited or timed out before authenticated readback: " + (logs.stdout + logs.stderr)[-2500:])
            time.sleep(0.3)
        else:
            raise AssertionError("fresh authenticated readback never reached the real child: " + call.stderr[-1000:])
        outcome = _wait_for_new_outcome(root)
        assert outcome["status"] == "succeeded", outcome
        assert len(counter.read_text().splitlines()) == 1
        assert (root / "oauth/oauth.sqlite3").read_bytes() == prior_oauth
        # A killed, already-successful owner must not turn its original
        # consumed first-start claim back into Docker launch authority.
        subprocess.run(["docker", "kill", "--signal", "KILL", container_id], check=True, capture_output=True, timeout=20)
        after_crash = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=20)
        assert after_crash.returncode == 1 and after_crash.stdout == ""
        assert len(counter.read_text().splitlines()) == 1
        assert json.loads((root / "state/new-first-outcome.json").read_bytes())["status"] == "succeeded"
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=30)


@pytest.mark.parametrize("unacceptable_readback", ["revoked-grant", "stale-observation"])
def test_real_new_first_launch_never_reports_readiness_from_invalid_readback(request_file: Path, unacceptable_readback: str) -> None:
    root = request_file.parent
    token, request = _prepare_real_new_source(request_file)
    bootstrap_path = Path(str(request["bootstrap_config"]))
    bootstrap = json.loads(bootstrap_path.read_bytes())
    bootstrap["verification_timeout"] = 25
    bootstrap_path.write_text(json.dumps(bootstrap))
    if unacceptable_readback == "revoked-grant":
        with closing(sqlite3.connect(root / "oauth/oauth.sqlite3")) as connection:
            connection.execute("UPDATE grants SET revoked=? WHERE client_id=?", (time.time(), "new-client"))
            connection.commit()
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    retained_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0

    tag = ""
    container_id = ""
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request)
        original = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert original.returncode == 0, original.stderr
        launched = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=150)
        assert launched.returncode == 0, launched.stderr
        assert json.loads(launched.stdout)["status"] == "first-child-observed-awaiting-authenticated-readback"
        actual_id = json.loads((root / "state/new-first-launched.json").read_bytes())["observation_id"]
        tested_id = "0" * 32 if unacceptable_readback == "stale-observation" else actual_id
        assert tested_id != actual_id or unacceptable_readback == "revoked-grant"
        expected_status = 401 if unacceptable_readback == "revoked-grant" else 200

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            readback = _authorized_readback(container_id, token, tested_id, expected_status=expected_status)
            if readback.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("real NEW invalid readback could not be tested: " + readback.stderr[-1200:])

        assert _wait_for_new_outcome(root, timeout=35)["status"] == "unknown"
        assert len((root / "state/physical-child-invocations").read_text().splitlines()) == 1
        assert (root / "oauth/oauth.sqlite3").read_bytes() == retained_oauth

        retry = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=20)
        assert retry.returncode == 1
        assert len((root / "state/physical-child-invocations").read_text().splitlines()) == 1
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=30)


@pytest.mark.parametrize("changed_original", ["bootstrap_config", "executable", "application_root"])
def test_real_new_start_refuses_changed_original_with_zero_docker_launches(request_file: Path, changed_original: str) -> None:
    root = request_file.parent
    _token, request = _prepare_real_new_source(request_file)
    retained_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0

    tag = ""
    container_id = ""
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request)
        original = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id)
        assert original.returncode == 0, original.stderr
        selected = Path(str(request[changed_original]))
        if changed_original == "application_root":
            selected /= "__init__.py"
        selected.write_bytes(selected.read_bytes() + b"\n# changed after durable original\n")

        refused = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=30)
        assert refused.returncode == 1 and refused.stdout == "", (refused.stdout, refused.stderr)
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
        assert not (root / "guard/new-docker-first-start.json").exists()
        assert not (root / "state/physical-child-invocations").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        assert (root / "oauth/oauth.sqlite3").read_bytes() == retained_oauth
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=30)


_PAUSE_AFTER_HOST_CLAIM = """
import json, os, signal, sys
from pathlib import Path
from auto_coder.management import fresh_new_host_start as host
original = host._create_host_start_claim
def pause_after_durable_claim(*args):
    digest = original(*args)
    print('new-host-claim-fsynced', flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)
    return digest
host._create_host_start_claim = pause_after_durable_claim
print(json.dumps(host.start_new_docker_once(Path(sys.argv[1]), sys.argv[2], sys.argv[3])), flush=True)
"""


_PAUSE_BEFORE_ENGINE_START = """
import json, os, signal, sys
from pathlib import Path
from auto_coder.management import fresh_new_host_start as host
original = host._start_docker_container_once
def pause_before_engine_start(*args):
    print('before-native-docker-start', flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)
    return original(*args)
host._start_docker_container_once = pause_before_engine_start
print(json.dumps(host.start_new_docker_once(Path(sys.argv[1]), sys.argv[2], sys.argv[3])), flush=True)
"""


def test_new_docker_refuses_external_first_start_racing_final_engine_command(request_file: Path) -> None:
    """A CLI-success no-op cannot authorize another process's Docker start."""
    root = request_file.parent
    _token, request = _prepare_real_new_source(request_file)
    original_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    original_intent = (root / "guard/new-instance-origin.json").read_bytes()
    tag = ""
    container_id = ""
    suspended = None
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request)
        assert _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id).returncode == 0
        suspended = subprocess.Popen(
            [sys.executable, "-B", "-c", _PAUSE_BEFORE_ENGINE_START, str(request_file), container_id, image_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_environment(),
        )
        assert suspended.stdout is not None
        assert select.select([suspended.stdout], [], [], 20)[0], "the host did not reach the last Engine command"
        assert suspended.stdout.readline() == "before-native-docker-start\n"
        original_claim = (root / "guard/new-docker-first-start.json").read_bytes()
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"

        # This independent Docker CLI invocation starts the selected PID-1,
        # but must not be mistaken for the host's atomic original start.
        outside_start = subprocess.run(["docker", "start", container_id], capture_output=True, text=True, timeout=35)
        assert outside_start.returncode == 0, outside_start.stderr
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", container_id], text=True).strip() == "true"

        os.kill(suspended.pid, signal.SIGCONT)
        stdout, stderr = suspended.communicate(timeout=35)
        assert suspended.returncode == 1 and stdout == "", (suspended.returncode, stdout, stderr)
        assert "did not perform an original start" in stderr
        assert (root / "guard/new-docker-first-start.json").read_bytes() == original_claim
        assert (root / "guard/new-instance-origin.json").read_bytes() == original_intent
        assert (root / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        assert not (root / "state/physical-child-invocations").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        replay = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=25)
        assert replay.returncode == 1 and replay.stdout == ""
        assert (root / "guard/new-docker-first-start.json").read_bytes() == original_claim
        assert not (root / "state/physical-child-invocations").exists()
    finally:
        if suspended is not None and suspended.poll() is None:
            suspended.kill()
            suspended.wait(timeout=10)
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)


def test_new_docker_rejects_restart_policy_added_after_durable_claim(request_file: Path) -> None:
    """A restart controller added during held admission must poison first start."""
    root = request_file.parent
    _token, request = _prepare_real_new_source(request_file)
    original_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    original_intent = (root / "guard/new-instance-origin.json").read_bytes()
    tag = ""
    container_id = ""
    suspended = None
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request)
        assert _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id).returncode == 0
        suspended = subprocess.Popen(
            [sys.executable, "-B", "-c", _PAUSE_AFTER_HOST_CLAIM, str(request_file), container_id, image_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_environment(),
        )
        assert suspended.stdout is not None
        assert select.select([suspended.stdout], [], [], 20)[0], "the host did not reach the durable claim barrier"
        assert suspended.stdout.readline() == "new-host-claim-fsynced\n"
        original_claim = (root / "guard/new-docker-first-start.json").read_bytes()
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
        subprocess.run(["docker", "update", "--restart=always", container_id], check=True, capture_output=True, timeout=25)
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}", container_id], text=True).strip() == "created always"

        os.kill(suspended.pid, signal.SIGCONT)
        stdout, stderr = suspended.communicate(timeout=35)
        assert suspended.returncode == 1 and stdout == "", (suspended.returncode, stdout, stderr)
        assert "static startup or restart policy changed" in stderr
        assert (root / "guard/new-docker-first-start.json").read_bytes() == original_claim
        assert (root / "guard/new-instance-origin.json").read_bytes() == original_intent
        assert (root / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        assert not (root / "state/physical-child-invocations").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"

        subprocess.run(["docker", "update", "--restart=no", container_id], check=True, capture_output=True, timeout=25)
        replay = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=25)
        assert replay.returncode == 1 and replay.stdout == ""
        assert (root / "guard/new-docker-first-start.json").read_bytes() == original_claim
        assert not (root / "state/physical-child-invocations").exists()
    finally:
        if suspended is not None and suspended.poll() is None:
            suspended.kill()
            suspended.wait(timeout=10)
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)


def test_new_docker_rejects_prior_process_joining_network_after_durable_claim(request_file: Path) -> None:
    """Recheck real bridge membership after a competing process enters mid-admission."""
    root = request_file.parent
    network = "ac-new-" + uuid.uuid4().hex[:24]
    port = _reserve_test_host_port()
    _token, request = _prepare_real_new_source(request_file, docker_network=network, host_port=port)
    original_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    original_intent = (root / "guard/new-instance-origin.json").read_bytes()
    tag = ""
    container_id = ""
    prior_id = ""
    suspended = None
    _new_isolated_bridge(network)
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request, docker_network=network, host_port=port)
        assert _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id).returncode == 0
        suspended = subprocess.Popen(
            [sys.executable, "-B", "-c", _PAUSE_AFTER_HOST_CLAIM, str(request_file), container_id, image_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_environment(),
        )
        assert suspended.stdout is not None
        assert select.select([suspended.stdout], [], [], 20)[0], "the host did not reach the durable claim barrier"
        assert suspended.stdout.readline() == "new-host-claim-fsynced\n"
        original_claim = (root / "guard/new-docker-first-start.json").read_bytes()
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"

        prior_id = subprocess.check_output(
            [
                "docker",
                "run",
                "--detach",
                "--network",
                network,
                "--restart",
                "no",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--entrypoint",
                "python3",
                os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544"),
                "-B",
                "-c",
                "import time;time.sleep(180)",
            ],
            text=True,
            timeout=40,
        ).strip()
        assert len(prior_id) == 64
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", prior_id], text=True).strip() == "true"
        os.kill(suspended.pid, signal.SIGCONT)
        stdout, stderr = suspended.communicate(timeout=35)
        assert suspended.returncode == 1 and stdout == "", (suspended.returncode, stdout, stderr)
        assert "network scope differs" in stderr
        assert (root / "guard/new-docker-first-start.json").read_bytes() == original_claim
        assert (root / "guard/new-instance-origin.json").read_bytes() == original_intent
        assert (root / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        assert not (root / "state/physical-child-invocations").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"

        subprocess.run(["docker", "rm", "--force", prior_id], check=True, capture_output=True, timeout=30)
        prior_id = ""
        replay = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=25)
        assert replay.returncode == 1 and replay.stdout == ""
        assert (root / "guard/new-docker-first-start.json").read_bytes() == original_claim
        assert not (root / "state/physical-child-invocations").exists()
    finally:
        if suspended is not None and suspended.poll() is None:
            suspended.kill()
            suspended.wait(timeout=10)
        if prior_id:
            subprocess.run(["docker", "rm", "--force", prior_id], capture_output=True, timeout=30, check=False)
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], check=True, capture_output=True, timeout=30)


def test_killed_first_host_preserves_consumed_claim_without_docker_start(request_file: Path) -> None:
    root = request_file.parent
    _token, request = _prepare_real_new_source(request_file)
    preserved_oauth = (root / "oauth/oauth.sqlite3").read_bytes()
    assert _run_host("begin", "--request-file", str(request_file)).returncode == 0
    tag = ""
    container_id = ""
    suspended = None
    try:
        container_id, image_id, tag = _create_real_new_container(root, request_file, request)
        assert _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id).returncode == 0
        suspended = subprocess.Popen(
            [sys.executable, "-B", "-c", _PAUSE_AFTER_HOST_CLAIM, str(request_file), container_id, image_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_environment(),
        )
        assert suspended.stdout is not None
        assert select.select([suspended.stdout], [], [], 20)[0], "first host did not reach the durable claim barrier"
        assert suspended.stdout.readline() == "new-host-claim-fsynced\n"
        claim = root / "guard/new-docker-first-start.json"
        before = claim.read_bytes()
        assert len(before) > 100
        os.kill(suspended.pid, signal.SIGKILL)
        assert suspended.wait(timeout=10) == -signal.SIGKILL
        retry = _run_host("start-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id, timeout=20)
        assert retry.returncode == 1, retry.stdout
        assert claim.read_bytes() == before
        assert subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id], text=True).strip() == "created"
        assert not (root / "state/physical-child-invocations").exists()
        assert not (root / "state/new-first-launch-claimed.json").exists()
        assert (root / "oauth/oauth.sqlite3").read_bytes() == preserved_oauth
    finally:
        if suspended is not None and suspended.poll() is None:
            suspended.kill()
            suspended.wait(timeout=10)
        if container_id:
            subprocess.run(["docker", "rm", "--force", container_id], capture_output=True, timeout=30)
        if tag:
            subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=30)
