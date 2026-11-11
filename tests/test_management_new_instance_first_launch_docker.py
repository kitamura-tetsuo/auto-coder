"""Real isolated Docker NEW owner, child and retained-grant readiness oracle."""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
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


def _required_original(path: Path) -> dict[str, object]:
    return json.loads(path.read_bytes())


def _prepare_real_new_source(request_file: Path) -> tuple[str, dict[str, object]]:
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
    origin = "http://127.0.0.1:18881"
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
        "listener_url": origin + "/mcp",
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
                "bind_host": "127.0.0.1",
                "port": 18881,
                "local_development": True,
            }
        )
    )
    return token, request


def _create_real_new_container(root: Path, request_file: Path, request: dict[str, object]) -> tuple[str, str, str]:
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
        "none",
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
            if outcome_path.exists() and json.loads(outcome_path.read_bytes()).get("status") == "unknown":
                logs = subprocess.run(["docker", "logs", "--tail", "40", container_id], capture_output=True, text=True, timeout=10)
                raise AssertionError("real child exited or timed out before authenticated readback: " + (logs.stdout + logs.stderr)[-2500:])
            time.sleep(0.3)
        else:
            raise AssertionError("fresh authenticated readback never reached the real child: " + call.stderr[-1000:])
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not (root / "state/new-first-outcome.json").exists():
            time.sleep(0.1)
        outcome = json.loads((root / "state/new-first-outcome.json").read_bytes())
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

        outcome_file = root / "state/new-first-outcome.json"
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline and not outcome_file.exists():
            time.sleep(0.1)
        assert outcome_file.is_file(), "NEW owner did not record the readback timeout"
        assert json.loads(outcome_file.read_bytes())["status"] == "unknown"
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
