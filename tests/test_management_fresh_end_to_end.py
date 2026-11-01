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
import re
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


def _resolve_fixture_base(configured: str) -> str:
    image = subprocess.check_output(["docker", "image", "inspect", configured, "--format", "{{.Id}}"], text=True).strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("fixture base is not an immutable image ID")
    return image


def _fixture_executable_digest(image: str) -> str:
    return subprocess.check_output(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sha256sum", image, "/opt/auto-coder/.venv/bin/auto-coder"], text=True).split()[0]


def _build_fixture_image(context: Path, image: str, tag: str, *, owner_token_gap: bool, contention: bool = False, crash_at: str | None = None) -> None:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("fixture base is not an immutable image ID")
    hooks = ""
    if owner_token_gap:
        hooks += """import os, sys
from pathlib import Path
from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor
original = ManagementBootstrapSupervisor._start_token
def observation(pid):
    if pid == os.getpid() and sys._getframe(2).f_code.co_name == "_start_fresh_initialization" and Path("/management-initialization/token-gap").exists():
        Path("/management-child-state/logs/token-gap-observed").touch()
        return ""
    return original(pid)
ManagementBootstrapSupervisor._start_token = staticmethod(observation)
"""
    if contention:
        hooks += """import fcntl, os, time
from pathlib import Path
from auto_coder.management import fresh_initialization
claim = fresh_initialization.request_and_claim_first_launch
def held_claim(*args, **kwargs):
    result = claim(*args, **kwargs)
    Path('/management-child-state/logs/claim-held').touch()
    while not Path('/management-initialization/release-claim').exists(): time.sleep(.01)
    return result
fresh_initialization.request_and_claim_first_launch = held_claim
flock = fcntl.flock
def observed_flock(fd, operation):
    label = os.environ.get('AUTOCODER_FRESH_COMPETITOR')
    if label and os.readlink('/proc/self/fd/' + str(fd)).endswith('/.supervisor-owner.lock'):
        Path('/management-child-state/logs/competitor-' + label + '-ready').write_text(str(os.getpid()))
        while not Path('/management-initialization/release-competitor-' + label).exists(): time.sleep(.01)
    return flock(fd, operation)
fcntl.flock = observed_flock
"""
    if crash_at:
        hooks += f"""import time
from pathlib import Path
from auto_coder.management import fresh_initialization
from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor
phase = {crash_at!r}
def hold():
    Path('/management-child-state/logs/crash-ready').touch()
    while not Path('/management-initialization/release-crash').exists(): time.sleep(.01)
claim = fresh_initialization.request_and_claim_first_launch
grant = fresh_initialization.request_fresh_host_grant
def held_grant(*args, **kwargs):
    result = grant(*args, **kwargs)
    if phase == 'before-claim': hold()
    return result
fresh_initialization.request_fresh_host_grant = held_grant
def held_claim(*args, **kwargs):
    result = claim(*args, **kwargs)
    if phase == 'after-claim': hold()
    return result
fresh_initialization.request_and_claim_first_launch = held_claim
record = fresh_initialization.record_fresh_launch
def held_record(*args, **kwargs):
    if phase == 'after-launch': hold()
    return record(*args, **kwargs)
fresh_initialization.record_fresh_launch = held_record
run = ManagementBootstrapSupervisor.run
def held_reconstruction(self):
    if Path('/management-initialization/hold-reconstruction').exists():
        Path('/management-child-state/logs/reconstruction-ready').touch()
        while not Path('/management-initialization/release-reconstruction').exists(): time.sleep(.01)
    return run(self)
ManagementBootstrapSupervisor.run = held_reconstruction
"""
    if hooks:
        (context / "sitecustomize.py").write_text(hooks)
    container = subprocess.check_output(["docker", "create", "--network", "none", image], text=True, timeout=30).strip()
    try:
        actual = subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", container], text=True, timeout=30).strip()
        if actual != image:
            raise ValueError("fixture container did not use the resolved image")
        target = container + ":/opt/auto-coder/.venv/lib/python3.12/site-packages"
        subprocess.run(["docker", "cp", str(context / "auto_coder") + "/.", target + "/auto_coder/"], check=True, capture_output=True, timeout=120)
        if hooks:
            subprocess.run(["docker", "cp", str(context / "sitecustomize.py"), target + "/sitecustomize.py"], check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "commit", "--change", 'ENTRYPOINT ["python", "-m", "auto_coder.management.bootstrap_supervisor"]', container, tag], check=True, capture_output=True, timeout=120)
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, check=False, timeout=30)


def test_mutable_base_tag_cannot_change_digest_or_derived_image(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("dedicated Docker fixture image is prepared for the host integration run")
    suffix = uuid.uuid4().hex
    tag_a, tag_b, derived = (f"auto-coder:fixture-{kind}-{suffix}" for kind in ("a", "b", "derived"))
    fixture_id = None
    try:
        for tag, extra in ((tag_a, f"LABEL fixture_identity={suffix}\n"), (tag_b, "USER root\nRUN printf '\\n# alternate fixture\\n' >> /opt/auto-coder/.venv/bin/auto-coder\n")):
            subprocess.run(["docker", "build", "--network", "none", "-t", tag, "-"], input="FROM auto-coder:management-local-abc65544\n" + extra, text=True, capture_output=True, check=True, timeout=120)
        selected = _resolve_fixture_base(tag_a)
        fixture_id = selected
        expected = _fixture_executable_digest(selected)
        alternate = _resolve_fixture_base(tag_b)
        assert selected != alternate and expected != _fixture_executable_digest(alternate)
        subprocess.run(["docker", "tag", tag_b, tag_a], check=True, capture_output=True, timeout=30)
        assert _resolve_fixture_base(tag_a) == alternate
        assert _fixture_executable_digest(selected) == expected
        context = tmp_path / "derived"
        (context / "auto_coder").mkdir(parents=True)
        (context / "auto_coder" / "fixture.txt").write_text("fixture")
        _build_fixture_image(context, selected, derived, owner_token_gap=False)
        actual_label = subprocess.check_output(["docker", "image", "inspect", derived, "--format", '{{index .Config.Labels "fixture_identity"}}'], text=True).strip()
        assert actual_label == suffix
        assert _fixture_executable_digest(_resolve_fixture_base(derived)) == expected
    finally:
        subprocess.run(["docker", "image", "rm", derived, tag_a, tag_b], capture_output=True, check=False, timeout=30)
        if fixture_id is not None:
            subprocess.run(["docker", "image", "rm", fixture_id], capture_output=True, check=False, timeout=30)


@pytest.mark.parametrize(
    "owner_token_gap, contention, crash_at, restart_after, prior_owner_locked",
    [
        (False, False, None, False, False),
        (True, False, None, False, False),
        (False, True, None, False, False),
        (False, False, "before-claim", False, False),
        (False, False, "after-claim", False, False),
        (False, False, "after-launch", False, False),
        (False, False, None, True, False),
        (False, False, None, True, True),
    ],
)
def test_fresh_first_launch_via_host_guard_and_authenticated_http(tmp_path: Path, owner_token_gap: bool, contention: bool, crash_at: str | None, restart_after: bool, prior_owner_locked: bool, record_property) -> None:
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

    base_image = _resolve_fixture_base(os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544"))
    executable_digest = _fixture_executable_digest(base_image)
    runtime = {"reference": "bootstrap", "receipt": "sha256:" + executable_digest, "executable": "/opt/auto-coder/.venv/bin/auto-coder", "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"], "environment": {"PYTHONDONTWRITEBYTECODE": "1"}}
    if restart_after:
        runtime["drain_ack"] = "/management-child-state/drain-bootstrap.json"
    common = {"state_root": "/management-child-state", "oauth_root": "/management-oauth", "journal_root": "/management-operations", "listener_url": origin + "/mcp", "initial_runtime": "bootstrap", "runtimes": [runtime]}
    (old_bootstrap / "config.json").write_text(json.dumps({**common, "instance_id": "old-fixture", "crash_budget": 0}))
    config = bootstrap / "config.json"
    config.write_text(json.dumps({**common, "instance_id": "fresh-fixture", "fresh_guard_path": "/management-initialization/fresh-guard.sock", "fresh_prior_lock_path": "/management-prior-owner.lock", "restart_after_clean_shutdown": restart_after}))
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
    tag = "auto-coder-fresh-e2e:" + uuid.uuid4().hex
    names = ["fresh-e2e-" + uuid.uuid4().hex[:12] for _ in range(3)]
    guard_process = None
    tracer = None
    competitor = None
    competitor_tracer = None
    fence = None
    network_fd = None
    try:
        _build_fixture_image(context, base_image, tag, owner_token_gap=owner_token_gap, contention=contention, crash_at=crash_at)
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
        if owner_token_gap or contention or crash_at:
            (state / "logs").mkdir()
        if owner_token_gap:
            (guard / "token-gap").touch()
        # Pause the real host guard immediately before its final socket check,
        # so a host-side syscall observer attaches before any child can launch.
        gate_ready, gate_open, trace = root / "trace-ready", root / "trace-go", root / "exec-trace"
        gate_script = """
import json, pathlib, sys, time
from auto_coder.management import fresh_host, fresh_host_observation as o
original, inspect = o.assert_replacement_published_listener, o._inspect
observe_network = o.assert_prior_network_unreferenced
def observed_network(namespace):
    try:
        return observe_network(namespace)
    except o.HostObservationUnavailable as e:
        cause = e.__cause__
        detail = str(cause) if isinstance(cause, ValueError) else type(cause).__name__ + ' errno=' + str(getattr(cause, 'errno', None))
        print('network namespace observation: ' + detail, file=sys.stderr, flush=True)
        raise
o.assert_prior_network_unreferenced = observed_network
ready, go = sys.argv[1:3]
def gated(intent):
    samples = []
    def observed(container_id):
        value = inspect(container_id)
        samples.append(value[11])
        return value
    pathlib.Path(ready).touch()
    while not pathlib.Path(go).exists(): time.sleep(.01)
    o._inspect = observed
    try:
        original(intent)
    except o.HostObservationUnavailable as e:
        cause = e.__cause__
        if isinstance(cause, ValueError): print('listener observation: '+str(cause), file=sys.stderr, flush=True)
        else: print('listener observation: '+type(cause).__name__+' errno='+str(getattr(cause, 'errno', None)), file=sys.stderr, flush=True)
        raise
    finally:
        o._inspect = inspect
        category = 'unavailable'
        if len(samples) == 2 and all(isinstance(x, list) for x in samples):
            canonical = lambda x: sorted(json.dumps(m, sort_keys=True, separators=(',', ':')) for m in x)
            category = 'identical' if samples[0] == samples[1] else ('order-only' if canonical(samples[0]) == canonical(samples[1]) else 'content-changed')
        print('mount comparison: '+category, file=sys.stderr, flush=True)
o.assert_replacement_published_listener = gated
sys.argv = [sys.argv[0], *sys.argv[3:]]
fresh_host.main()
"""
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
        if owner_token_gap:
            observed = state / "logs" / "token-gap-observed"
            until = time.monotonic() + 30
            while not observed.exists():
                if time.monotonic() >= until:
                    raise AssertionError("fresh owner-token gap was not reached")
                time.sleep(0.05)
            pid = int(subprocess.check_output(["docker", "inspect", "--format", "{{.State.Pid}}", new_id], text=True).strip())
            group = Path("/sys/fs/cgroup/system.slice") / ("docker-" + new_id + ".scope")
            for _ in range(4):
                assert (group / "cgroup.procs").read_text().split() == [str(pid)]
                assert not (state / "fresh-launch-claimed.json").exists()
                assert not (state / "management-child.sqlite3").exists()
                assert not list(journal.iterdir())
                time.sleep(0.2)
            (guard / "token-gap").unlink()
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
        categories = [line.removeprefix("mount comparison: ") for line in err.splitlines() if line.startswith("mount comparison: ")]
        if len(categories) == 1 and categories[0] in {"identical", "order-only", "content-changed", "unavailable"}:
            record_property("mount_comparison", categories[0])
        assert guard_process.returncode == 0, (out, err)

        if crash_at:

            def retained_status():
                probe = "import json; from pathlib import Path; from auto_coder.management.fresh_initialization import read_fresh_status; print(json.dumps(read_fresh_status(Path('/management-child-state'))))"
                output = subprocess.check_output(
                    ["docker", "run", "--rm", "--network", "none", "--read-only", "--mount", f"type=bind,src={state},dst=/management-child-state,readonly", "--entrypoint", "python", image, "-c", probe],
                    text=True,
                    timeout=30,
                )
                return json.loads(output.splitlines()[-1])

            crash_ready = state / "logs" / "crash-ready"
            until = time.monotonic() + 60
            while not crash_ready.exists():
                assert time.monotonic() < until, "fresh owner did not reach the requested crash boundary"
                time.sleep(0.02)
            claim_path = state / "fresh-launch-claimed.json"
            assert claim_path.exists() is (crash_at != "before-claim")
            assert not (state / "fresh-launched.json").exists()
            subprocess.run(["docker", "kill", new_id], check=True, capture_output=True, timeout=30)
            tracer.wait(timeout=10)
            tracer = None
            starts = [line for line in trace.read_text().splitlines() if 'execve("/opt/auto-coder/.venv/bin/auto-coder"' in line]
            assert len(starts) == (1 if crash_at == "after-launch" else 0), starts
            assert retained_status()["status"] == "unknown"

            (guard / "hold-reconstruction").touch()
            subprocess.run(["docker", "start", new_id], check=True, capture_output=True, timeout=30)
            restart_ready = state / "logs" / "reconstruction-ready"
            until = time.monotonic() + 30
            while not restart_ready.exists():
                running = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", new_id], text=True).strip()
                assert running == "true" and time.monotonic() < until, "reconstruction did not reach its trace barrier"
                time.sleep(0.02)
            restart_pid = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Pid}}", new_id], text=True).strip()
            restart_trace = root / "reconstruction-exec-trace"
            tracer = subprocess.Popen(["strace", "-f", "-e", "trace=execve", "-o", str(restart_trace), "-p", restart_pid], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            assert tracer.stderr is not None and select.select([tracer.stderr], [], [], 10)[0]
            assert "attached" in tracer.stderr.readline()
            (guard / "release-reconstruction").touch()
            exit_code = subprocess.check_output(["docker", "wait", new_id], text=True, timeout=60).strip()
            assert exit_code != "0"
            tracer.wait(timeout=10)
            tracer = None
            assert 'execve("/opt/auto-coder/.venv/bin/auto-coder"' not in restart_trace.read_text()
            assert retained_status()["status"] == "unknown"
            assert grant_history() == retained_grants
            assert {str(path.relative_to(old_state)): hashlib.sha256(path.read_bytes()).hexdigest() for path in old_state.rglob("*") if path.is_file()} == old_history
            return

        def assert_refused_competitor(label: str) -> None:
            nonlocal competitor, competitor_tracer
            competitor = subprocess.Popen(
                ["docker", "exec", "-e", f"AUTOCODER_FRESH_COMPETITOR={label}", new_id, "python", "-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json", "--fresh-initialization"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            competitor_ready = state / "logs" / f"competitor-{label}-ready"
            until = time.monotonic() + 30
            while not competitor_ready.exists():
                assert competitor.poll() is None and time.monotonic() < until, "competing owner did not reach the lock"
                time.sleep(0.02)
            namespace_pid = competitor_ready.read_text().strip()
            host_pids = subprocess.check_output(["docker", "top", new_id, "-eo", "pid"], text=True).splitlines()[1:]
            matches = []
            for candidate in host_pids:
                candidate = candidate.strip()
                status = Path("/proc", candidate, "status").read_text()
                nspid = next(line for line in status.splitlines() if line.startswith("NSpid:"))
                if nspid.split()[-1] == namespace_pid:
                    matches.append(candidate)
            assert len(matches) == 1
            competitor_trace = root / f"competitor-{label}-exec-trace"
            competitor_tracer = subprocess.Popen(["strace", "-f", "-e", "trace=execve", "-o", str(competitor_trace), "-p", matches[0]], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            assert competitor_tracer.stderr is not None and select.select([competitor_tracer.stderr], [], [], 10)[0]
            assert "attached" in competitor_tracer.stderr.readline()
            (guard / f"release-competitor-{label}").touch()
            competitor_out, competitor_err = competitor.communicate(timeout=30)
            assert competitor.returncode != 0, (competitor_out, competitor_err)
            assert "BlockingIOError" in competitor_err, (competitor_out, competitor_err)
            competitor_tracer.wait(timeout=10)
            competitor_tracer = None
            assert 'execve("/opt/auto-coder/.venv/bin/auto-coder"' not in competitor_trace.read_text()

        if contention:
            claim_held = state / "logs" / "claim-held"
            until = time.monotonic() + 30
            while not claim_held.exists():
                assert time.monotonic() < until, "first owner did not reach the durable claim barrier"
                time.sleep(0.02)
            claim = (state / "fresh-launch-claimed.json").read_bytes()
            assert not (state / "fresh-launched.json").exists()
            assert_refused_competitor("contended")
            assert (state / "fresh-launch-claimed.json").read_bytes() == claim
            assert not (state / "fresh-launched.json").exists()
            assert grant_history() == retained_grants
            (guard / "release-claim").touch()

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
        if contention:
            assert_refused_competitor("replay")
            assert (state / "fresh-launch-claimed.json").read_bytes() == claim
            assert status_inside_container()["status"] == "succeeded"
        tracer.send_signal(signal.SIGINT)
        tracer.wait(timeout=10)
        tracer = None
        starts = [line for line in trace.read_text().splitlines() if 'execve("/opt/auto-coder/.venv/bin/auto-coder"' in line]
        assert len(starts) == 1, starts
        if restart_after:
            with sqlite3.connect(state / "management-child.sqlite3") as db:
                previous = json.loads(db.execute("SELECT record_json FROM child_state").fetchone()[0])
            # Drop the host test fence so only the successor can hold the old
            # owner out; the kernel lock is checked again after reconstruction.
            assert fence is not None
            os.close(fence)
            fence = None
            subprocess.run(["docker", "stop", "--timeout", "40", new_id], check=True, capture_output=True, timeout=50)
            probe_lock = os.open(old_lock, os.O_RDONLY)
            fcntl.flock(probe_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(state / "management-child.sqlite3") as db:
                stopped = json.loads(db.execute("SELECT record_json FROM child_state").fetchone()[0])
            assert stopped["phase"] == "stopped" and stopped["intended_stop"] is True
            if prior_owner_locked:
                subprocess.run(["docker", "start", new_id], check=True, capture_output=True, timeout=30)
                assert subprocess.check_output(["docker", "wait", new_id], text=True, timeout=30).strip() != "0"
                with sqlite3.connect(state / "management-child.sqlite3") as db:
                    assert db.execute("SELECT count(*) FROM effects WHERE action='START_SELECTED'").fetchone()[0] == 1
            fcntl.flock(probe_lock, fcntl.LOCK_UN)
            os.close(probe_lock)
            subprocess.run(["docker", "start", new_id], check=True, capture_output=True, timeout=30)
            end = time.monotonic() + 60
            while True:
                with sqlite3.connect(state / "management-child.sqlite3") as db:
                    new = json.loads(db.execute("SELECT record_json FROM child_state").fetchone()[0])
                    starts_after = db.execute("SELECT count(*) FROM effects WHERE action='START_SELECTED'").fetchone()[0]
                reply = subprocess.run(["docker", "exec", "--interactive", new_id, "python", "-c", script], input=json.dumps({"request": request, "token": token}), text=True, capture_output=True, check=False, timeout=30)
                if reply.returncode == 0 and new["phase"] == "running":
                    break
                assert time.monotonic() < end, "restarted management child never became authenticated"
                time.sleep(0.2)
            assert new["child_generation"] == previous["child_generation"] + 1
            assert new["supervisor_incarnation"] != previous["supervisor_incarnation"]
            assert starts_after == 2
            probe_lock = os.open(old_lock, os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe_lock)
            assert json.loads(reply.stdout)["result"]["structuredContent"]["uid"] == "operator"
            # Fresh request and independent local verifier must join the new
            # child, not merely echo the initial authenticated HTTP response.
            observation_id = uuid.uuid4().hex
            observation_start = time.time()
            request["params"]["arguments"] = {"observation_id": observation_id}
            reply = subprocess.run(["docker", "exec", "--interactive", new_id, "python", "-c", script], input=json.dumps({"request": request, "token": token}), text=True, capture_output=True, check=True, timeout=30)
            assert json.loads(reply.stdout)["result"]["structuredContent"]["uid"] == "operator"
            proof_probe = (
                "from pathlib import Path; import sys; "
                "from auto_coder.management.bootstrap_supervisor import BootstrapConfig; "
                "from auto_coder.management.readiness_observation import read_authenticated_readback; "
                "c=BootstrapConfig.read(Path('/bootstrap/config.json')); "
                "p=read_authenticated_readback(c,observation_id=sys.argv[1],"
                "observation_started_at=float(sys.argv[2]),observation_ends_at=float(sys.argv[2])+60); "
                "print(p.child_identity if p else 'UNAVAILABLE')"
            )
            proof = subprocess.check_output(["docker", "exec", new_id, "python", "-c", proof_probe, observation_id, str(observation_start)], text=True, timeout=15).strip()
            assert proof != "UNAVAILABLE" and proof.startswith(str(new["pid"]) + ":" + new["start_token"] + ":")
            assert status_inside_container()["status"] == "succeeded"
        assert grant_history() == retained_grants
        assert {str(path.relative_to(old_state)): hashlib.sha256(path.read_bytes()).hexdigest() for path in old_state.rglob("*") if path.is_file()} == old_history
    finally:
        if competitor is not None and competitor.poll() is None:
            competitor.terminate()
            competitor.wait(timeout=10)
        if competitor_tracer is not None and competitor_tracer.poll() is None:
            competitor_tracer.send_signal(signal.SIGINT)
            competitor_tracer.wait(timeout=10)
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
