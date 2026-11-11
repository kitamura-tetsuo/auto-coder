"""Real Docker/OAuth fixtures shared by the isolated private-Engine tests.

The original host Docker first-start suite was superseded by a private Engine
requirement. These helpers do not claim that the outer Engine can launch NEW.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

import yaml

from auto_coder.management.action_scopes import READ_SCOPE
from auto_coder.management.identity import AdmissionResult, AdmissionStatus, ReasonCode, VerifiedPrincipal
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import AuthorizationRequest, OAuthGrantService
from auto_coder.management.oauth_store import OAuthStore
from tests.test_management_new_instance_preflight import _environment


def _run_host(*arguments: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.management.fresh_host", *arguments],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for_new_outcome(root: Path, timeout: float = 40) -> dict[str, object]:
    """Wait for the fully fsynced result, not just the first file inode."""
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


def _prepare_real_new_source(request_file: Path) -> tuple[str, dict[str, object]]:
    root = request_file.parent
    request = json.loads(request_file.read_bytes())
    application = root / "application" / "auto_coder"
    application.parent.mkdir(mode=0o700)
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "src" / "auto_coder",
        application,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    request["application_root"] = str(application)
    request["new_instance"] = "new-docker-" + uuid.uuid4().hex[:12]
    request["request_id"] = str(uuid.uuid4())
    request["runtime_reference"] = "new-management"
    request_file.write_text(json.dumps(request, sort_keys=True))

    selected_executable = Path(str(request["executable"]))
    counter = root / "state" / "physical-child-invocations"
    selected_executable.write_text(
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
    pending = grants.begin_authorization(
        AuthorizationRequest("new-client", origin + "/callback", settings.resource, challenge, (READ_SCOPE,)),
        "browser",
        "csrf",
    )
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


def _create_real_new_container(root: Path, request_file: Path, request: dict[str, object], *, wrong_command: bool = False) -> tuple[str, str, str]:
    image_base = os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544")
    dockerfile = root / "Dockerfile"
    dockerfile.write_text(f'FROM {image_base}\nENTRYPOINT ["python3", "-B", "-m", "auto_coder.management.bootstrap_supervisor"]\n')
    tag = "auto-coder-new-first:" + uuid.uuid4().hex
    subprocess.run(
        ["docker", "build", "--network", "none", "-t", tag, str(root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    image_id = subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True).strip()
    command = [
        "docker",
        "create",
        "--name",
        "auto-coder-new-first-" + uuid.uuid4().hex[:14],
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
    for key in (
        "state_root",
        "operation_root",
        "guard_root",
        "oauth_root",
        "bootstrap_config",
        "management_config",
        "executable",
        "application_root",
    ):
        path = str(request[key])
        readonly = ",readonly" if key in {"bootstrap_config", "management_config", "executable", "application_root"} else ""
        command.extend(["--mount", f"type=bind,src={path},dst={path}{readonly}"])
    command.extend(["--mount", f"type=bind,src={request_file},dst={request_file},readonly"])
    if wrong_command:
        command.extend(["--entrypoint", "/bin/sh", image_id, "-c", "true"])
    else:
        command.extend([image_id, "--config", str(request["bootstrap_config"]), "--new-initialization", "--request-file", str(request_file)])
    return subprocess.check_output(command, text=True, timeout=40).strip(), image_id, tag


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
    return subprocess.run(
        ["docker", "exec", "--env", "SYNTHETIC_TOKEN=" + token, container_id, "python3", "-B", "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
    )


PAUSE_AFTER_HOST_CLAIM = """
import json, os, signal, sys
from pathlib import Path
from auto_coder.management import fresh_new_host_start as host
original = host._create_host_start_claim
def pause_after_durable_claim(*args, **kwargs):
    digest = original(*args, **kwargs)
    print('new-host-claim-fsynced', flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)
    return digest
host._create_host_start_claim = pause_after_durable_claim
if len(sys.argv) != 6:
    raise SystemExit('private original NEW arguments required')
print(json.dumps(host.start_new_docker_once(Path(sys.argv[1]), sys.argv[2], sys.argv[3], private_root=Path(sys.argv[4]), outer_socket=Path(sys.argv[5]))), flush=True)
"""
