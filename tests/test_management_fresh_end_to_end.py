"""Disposable host-guard-to-authenticated-child fresh initialization fixture.

Requires a host-root test runner to observe root-owned Docker proxy descriptors.
All containers, credentials, ports and stores are created solely for this test.
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
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from auto_coder.management.action_scopes import READ_SCOPE
from auto_coder.management.fresh_host import _write_evidence
from auto_coder.management.fresh_host_observation import capture_prior_container
from auto_coder.management.fresh_initialization import FreshInitializationIntent
from auto_coder.management.identity import AdmissionResult, AdmissionStatus, ReasonCode, VerifiedPrincipal
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import AuthorizationRequest, OAuthGrantService

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


def test_fresh_first_launch_via_host_guard_and_authenticated_http(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("host root is required to observe the disposable Docker proxy and namespace evidence")
    root = tmp_path
    old_state, state, journal, oauth, guard, bootstrap, old_bootstrap = (root / name for name in ("old-state", "state", "journal", "oauth", "guard", "bootstrap", "old-bootstrap"))
    for directory in (old_state, state, journal, oauth, guard, bootstrap, old_bootstrap):
        directory.mkdir(mode=0o700)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = "http://127.0.0.1:8081"
    client = OAuthClient("fixture-client", (origin + "/callback",))
    settings = OAuthSettings(oauth / "oauth.sqlite3", origin, "fresh-fixture", (client,))
    principal = VerifiedPrincipal("fresh-fixture", "operator", time.time() - 10)
    permitted = AdmissionResult(AdmissionStatus.PERMITTED, ReasonCode.PERMITTED, principal)

    class LocalIdentity:
        def verify_login(self, _token):
            return permitted

        def recheck_identity(self, _principal):
            return permitted

        def check_operator_policy(self, _principal):
            return permitted

    executable_digest = subprocess.check_output(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sha256sum", "auto-coder:management-local-abc65544", "/opt/auto-coder/.venv/bin/auto-coder"], text=True).split()[0]
    runtime = {"reference": "bootstrap", "receipt": "sha256:" + executable_digest, "executable": "/opt/auto-coder/.venv/bin/auto-coder", "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"], "environment": {"PYTHONDONTWRITEBYTECODE": "1"}}
    common = {"state_root": "/management-child-state", "oauth_root": "/management-oauth", "journal_root": "/management-operations", "listener_url": origin + "/mcp", "initial_runtime": "bootstrap", "runtimes": [runtime]}
    (old_bootstrap / "config.json").write_text(json.dumps({**common, "instance_id": "old-fixture", "crash_budget": 0}))
    config = bootstrap / "config.json"
    config.write_text(json.dumps({**common, "instance_id": "fresh-fixture", "fresh_guard_path": "/management-initialization/fresh-guard.sock", "fresh_prior_lock_path": "/management-prior-owner.lock"}))
    management = bootstrap / "management.yaml"
    management.write_text(
        yaml.safe_dump(
            {
                "state_root": "/management-oauth",
                "operation_state_root": "/management-operations",
                "bootstrap_config_path": "/bootstrap/config.json",
                "public_origin": origin,
                "firebase_project_id": "fresh-fixture",
                "clients": [{"client_id": client.client_id, "redirect_uris": list(client.redirect_uris)}],
                "bind_host": "127.0.0.1",
                "port": 8081,
                "local_development": True,
            }
        )
    )
    context = root / "image"
    context.mkdir()
    source = Path(__file__).resolve().parents[1] / "src" / "auto_coder"
    shutil.copytree(source, context / "auto_coder", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (context / "Dockerfile").write_text('FROM auto-coder:management-local-abc65544\nCOPY auto_coder/ /opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder/\nENTRYPOINT ["python", "-m", "auto_coder.management.bootstrap_supervisor"]\n')
    tag = "auto-coder-fresh-e2e:" + uuid.uuid4().hex
    names = ["fresh-e2e-" + uuid.uuid4().hex[:12] for _ in range(3)]
    guard_process = None
    tracer = None
    fence = None
    network_fd = None
    try:
        subprocess.run(["docker", "build", "--network", "none", "-t", tag, str(context)], check=True, capture_output=True, timeout=120)
        image = subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True).strip()
        old_mounts = [(old_state, "/management-child-state", True), (old_bootstrap, "/bootstrap", False), (oauth, "/management-oauth", True)]
        old_args = ["docker", "run", "-d", "--name", names[0], "--publish", f"127.0.0.1:{port}:8080"]
        for source_path, destination, writable in old_mounts:
            old_args += ["--mount", f"type=bind,src={source_path},dst={destination}" + ("" if writable else ",readonly")]
        old_id = subprocess.check_output(old_args + [image, "--config", "/bootstrap/config.json"], text=True).strip()
        deadline = time.monotonic() + 30
        while not (old_state / "management-child.sqlite3").is_file():
            if time.monotonic() >= deadline:
                raise AssertionError("disposable old supervisor did not initialize its ledger")
            time.sleep(0.05)
        grants = OAuthGrantService(settings, LocalIdentity())
        verifier = "v" * 48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        pending = grants.begin_authorization(AuthorizationRequest(client.client_id, origin + "/callback", settings.resource, challenge, (READ_SCOPE,)), "browser", "csrf")
        code = grants.approve_authorization(pending.request_id, "browser", "csrf", "fixture-login").code
        assert code is not None
        token = grants.exchange_code(client.client_id, code, origin + "/callback", settings.resource, verifier).access_token
        gc.collect()
        with sqlite3.connect(oauth / "oauth.sqlite3") as db:
            assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        (oauth / "allowed_uids.json").write_text(json.dumps({"project_id": "fresh-fixture", "allowed_uids": ["operator"]}))
        proxy_id = subprocess.check_output(["docker", "run", "-d", "--name", names[1], "--network", "container:" + old_id, "--entrypoint", "/bin/sleep", image, "120"], text=True).strip()
        deadline = time.monotonic() + 30
        while True:
            try:
                old = capture_prior_container(old_id, image, expected_mounts=tuple((str(a), b, c) for a, b, c in old_mounts), require_owner_lock=True, require_deployment_identity=True)
                break
            except ValueError:
                running = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", old_id], text=True).strip()
                if running != "true":
                    logs = subprocess.run(["docker", "logs", old_id], text=True, capture_output=True, check=False)
                    raise AssertionError("disposable old supervisor exited: " + (logs.stdout + logs.stderr)[-3000:])
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        proxy = capture_prior_container(proxy_id, image)
        network_fd = os.open(f"/proc/{old.pid}/ns/net", os.O_RDONLY)
        evidence_digest = _write_evidence(guard, {"management": asdict(old), "proxy": asdict(proxy)})
        subprocess.run(["docker", "stop", "--time", "1", proxy_id, old_id], check=True, capture_output=True, timeout=30)
        old_history = {str(path.relative_to(old_state)): hashlib.sha256(path.read_bytes()).hexdigest() for path in old_state.rglob("*") if path.is_file()}

        def grant_history():
            with sqlite3.connect((oauth / "oauth.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
                return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in ("authority", "grants", "access_tokens", "refresh_tokens")}

        retained_grants = grant_history()
        old_lock = old_state / ".supervisor-owner.lock"
        fence = os.open(old_lock, os.O_RDONLY)
        fcntl.flock(fence, fcntl.LOCK_SH | fcntl.LOCK_NB)
        new_args = ["docker", "create", "--name", names[2], "--read-only", "--publish", f"127.0.0.1:{port}:8080"]
        for source_path, destination, readonly in (
            (state, "/management-child-state", False),
            (journal, "/management-operations", False),
            (oauth, "/management-oauth", False),
            (bootstrap, "/bootstrap", True),
            (guard, "/management-initialization", True),
            (old_lock, "/management-prior-owner.lock", True),
        ):
            new_args += ["--mount", f"type=bind,src={source_path},dst={destination}" + (",readonly" if readonly else "")]
        new_id = subprocess.check_output(new_args + [image, "--config", "/bootstrap/config.json", "--fresh-initialization"], text=True).strip()
        probe = "from pathlib import Path; from auto_coder.management.fresh_initialization import application_tree_sha256; print(application_tree_sha256(Path('/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder')))"
        app_digest = subprocess.check_output(["docker", "run", "--rm", "--network", "none", "--read-only", "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", image, "-c", probe], text=True).strip().splitlines()[-1]
        s, j, o, g, lock = (path.stat() for path in (state, journal, oauth, guard, old_lock))
        intent = FreshInitializationIntent(
            str(uuid.uuid4()),
            "old-fixture",
            old_id,
            "fresh-fixture",
            new_id,
            evidence_digest,
            lock.st_dev,
            lock.st_ino,
            port,
            hashlib.sha256(config.read_bytes()).hexdigest(),
            hashlib.sha256(management.read_bytes()).hexdigest(),
            image,
            executable_digest,
            app_digest,
            "bootstrap",
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
        intent_file = root / "intent.json"
        intent_file.write_text(json.dumps(asdict(intent)))
        intent_file.chmod(0o600)
        base = [sys.executable, "-m", "auto_coder.management.fresh_host"]
        subprocess.run(base + ["claim", "--intent-file", str(intent_file), "--evidence-file", str(guard / "prior-evidence.json"), "--prior-lock", str(old_lock), "--prior-network-fd", str(network_fd)], pass_fds=(network_fd,), check=True, capture_output=True, timeout=30)
        # Pause the real host guard immediately before its final socket check,
        # so a host-side syscall observer attaches before any child can launch.
        gate_ready, gate_open, trace = root / "trace-ready", root / "trace-go", root / "exec-trace"
        gate_script = (
            "import pathlib,sys,time; from auto_coder.management import fresh_host, fresh_host_observation as o; "
            "original=o.assert_replacement_published_listener; ready,go=sys.argv[1:3]; "
            "exec('def gated(intent):\\n pathlib.Path(ready).touch()\\n while not pathlib.Path(go).exists(): time.sleep(.01)\\n original(intent)'); "
            "o.assert_replacement_published_listener=gated; sys.argv=[sys.argv[0],*sys.argv[3:]]; fresh_host.main()"
        )
        guard_process = subprocess.Popen(
            [sys.executable, "-c", gate_script, str(gate_ready), str(gate_open), "guard", "--state-root", str(state), "--evidence-file", str(guard / "prior-evidence.json"), "--prior-lock", str(old_lock), "--guard-dir", str(guard), "--prior-network-fd", str(network_fd)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(network_fd,),
        )
        assert guard_process.stdout is not None
        assert select.select([guard_process.stdout], [], [], 30)[0], "host guard did not become ready"
        assert json.loads(guard_process.stdout.readline())["status"] == "awaiting-replacement"
        subprocess.run(["docker", "start", new_id], check=True, capture_output=True, timeout=30)
        until = time.monotonic() + 30
        while not gate_ready.exists():
            running = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", new_id], text=True).strip()
            if time.monotonic() >= until or guard_process.poll() is not None or running != "true":
                logs = subprocess.run(["docker", "logs", new_id], text=True, capture_output=True, check=False)
                guard_error = guard_process.stderr.read() if guard_process.poll() is not None and guard_process.stderr is not None else ""
                raise AssertionError("host guard did not reach the trace barrier: " + (guard_error + logs.stdout + logs.stderr)[-3000:])
            time.sleep(0.05)
        pid = int(subprocess.check_output(["docker", "inspect", "--format", "{{.State.Pid}}", new_id], text=True).strip())
        tracer = subprocess.Popen(["strace", "-f", "-e", "trace=execve", "-o", str(trace), "-p", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        assert tracer.stderr is not None and select.select([tracer.stderr], [], [], 10)[0], "syscall observer failed to attach"
        assert "attached" in tracer.stderr.readline()
        gate_open.touch()
        out, err = guard_process.communicate(timeout=60)
        assert guard_process.returncode == 0, (out, err)

        def status_inside_container():
            probe_status = "import json; from pathlib import Path; from auto_coder.management.fresh_initialization import read_fresh_status; print(json.dumps(read_fresh_status(Path('/management-child-state'))))"
            return json.loads(subprocess.check_output(["docker", "exec", new_id, "python", "-c", probe_status], text=True, timeout=10).splitlines()[-1])

        deadline = time.monotonic() + 60
        while True:
            status = status_inside_container()
            if status["status"] == "launched":
                break
            assert status["status"] != "unknown"
            if time.monotonic() >= deadline:
                raise AssertionError("fresh launch did not reach authenticated readiness")
            time.sleep(0.1)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "auto_coder_whoami", "arguments": {"observation_id": status["observation_id"]}}}
        script = (
            "import json,sys,urllib.request; payload=json.load(sys.stdin); r=urllib.request.Request('http://127.0.0.1:8081/mcp',data=json.dumps(payload['request']).encode(),"
            "headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':'Bearer '+payload['token']}); "
            "print(urllib.request.urlopen(r,timeout=10).read().decode())"
        )
        while True:
            reply = subprocess.run(["docker", "exec", "--interactive", new_id, "python", "-c", script], input=json.dumps({"request": request, "token": token}), text=True, capture_output=True, check=False, timeout=30)
            if reply.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("disposable protected HTTP failed: " + reply.stderr[-1500:])
            time.sleep(0.2)
        result = reply.stdout
        assert json.loads(result)["result"]["structuredContent"]["uid"] == "operator"
        while status_inside_container()["status"] != "succeeded":
            if time.monotonic() >= deadline:
                raise AssertionError("authenticated child proof was not durably observed")
            time.sleep(0.1)
        tracer.send_signal(signal.SIGINT)
        tracer.wait(timeout=10)
        tracer = None
        starts = [line for line in trace.read_text().splitlines() if 'execve("/opt/auto-coder/.venv/bin/auto-coder"' in line]
        assert len(starts) == 1, starts
        assert grant_history() == retained_grants
        assert {str(path.relative_to(old_state)): hashlib.sha256(path.read_bytes()).hexdigest() for path in old_state.rglob("*") if path.is_file()} == old_history
    finally:
        if tracer is not None and tracer.poll() is None:
            tracer.send_signal(signal.SIGINT)
            tracer.wait(timeout=10)
        if guard_process is not None and guard_process.poll() is None:
            guard_process.terminate()
            guard_process.wait(timeout=10)
        if fence is not None:
            os.close(fence)
        if network_fd is not None:
            os.close(network_fd)
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True, check=False, timeout=40)
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False, timeout=30)
