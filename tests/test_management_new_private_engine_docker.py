"""Disposable runner acceptance for the trusted-host private Engine producer.

This creates one privileged Docker-in-Docker parent only when the dedicated
GitHub Actions job opts in. It never targets a production host or service.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import pytest

from auto_coder.management.fresh_begin import begin_new_instance
from auto_coder.management.fresh_host_observation import HostObservationUnavailable
from auto_coder.management.fresh_private_engine import (
    SOURCE_CREATED,
    SOURCE_INTENT,
    SOURCE_READY,
    assert_private_engine_live,
    create_private_engine,
    hold_private_engine_source,
)
from tests.support.new_first_start import (
    PAUSE_AFTER_HOST_CLAIM,
    _authorized_readback,
    _create_real_new_container,
    _prepare_real_new_source,
    _run_host,
    _wait_for_new_outcome,
)
from tests.test_management_new_instance_preflight import _environment, request_file
from tests.test_management_new_instance_profile import configured_request

pytestmark = [
    pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep"),
    pytest.mark.skipif(
        os.environ.get("AUTO_CODER_PRIVATE_ENGINE_ACCEPTANCE") != "1",
        reason="requires explicitly opted-in disposable runner Docker privileges",
    ),
]


def _remove_disposable_engine_root(root: Path) -> None:
    """Remove only the test-created /tmp root after the DinD parent exits.

    Docker takes root ownership of its data-root contents. A GitHub-hosted
    runner can remove that disposable subtree with sudo after the parent is
    removed, but no production path or unchecked caller path is accepted.
    """
    if root.parent != Path("/tmp") or not root.name.startswith("acpe-") or not root.is_dir() or root.is_symlink():
        raise AssertionError("refusing to clean a non-disposable private Engine root")
    try:
        shutil.rmtree(root)
    except PermissionError:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise
        subprocess.run(["sudo", "--non-interactive", "rm", "--recursive", "--force", "--", str(root)], check=True, timeout=30)


def _original_private_child_requires_bearer(container_id: str) -> bool:
    """Probe the real configured MCP endpoint in the exact child namespace."""
    script = (
        "import http.client,json;"
        "c=http.client.HTTPConnection('127.0.0.1',18881,timeout=3);"
        "p=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',"
        "'params':{'name':'auto_coder_whoami','arguments':{'observation_id':'0'*32}}}).encode();"
        "c.request('POST','/mcp',p,headers={'Content-Type':'application/json',"
        "'Accept':'application/json, text/event-stream'});"
        "r=c.getresponse();b=r.read(65537);"
        "assert r.status==401,(r.status,b);"
        "assert 'Bearer' in r.getheader('WWW-Authenticate',''),(r.status,b)"
    )
    probe = subprocess.run(["docker", "exec", container_id, "python3", "-B", "-c", script], capture_output=True, text=True, timeout=20)
    return probe.returncode == 0


def test_real_private_engine_parent_preserves_original_and_seals_independent_source(configured_request: Path) -> None:
    """Prove the exact parent Docker effect, socket custody, and live receipts."""
    original = begin_new_instance(configured_request)
    outer_socket = Path("/var/run/docker.sock")
    original_request = configured_request.read_bytes()
    outer = ["docker", "--host", f"unix://{outer_socket}"]
    image = subprocess.run(
        [*outer, "image", "inspect", "docker:29-dind", "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    # Keep the real Unix Engine pathname safely below Linux sun_path limits.
    root = Path(tempfile.mkdtemp(prefix="acpe-", dir="/tmp"))
    parent_id: str | None = None
    try:
        evidence = create_private_engine(configured_request, root, outer_socket, image, timeout=90)
        parent_id = evidence.parent_container_id
        assert evidence.origin_sha256 == original.record_sha256
        assert evidence.parent_image_id == image
        assert evidence.outer_engine_id != evidence.private_engine_id
        assert evidence.socket_ino > 0
        assert configured_request.read_bytes() == original_request
        assert {entry.name for entry in root.iterdir()} == {
            "socket",
            "data",
            SOURCE_INTENT,
            SOURCE_CREATED,
            SOURCE_READY,
        }
        with hold_private_engine_source(configured_request, root, outer_socket) as observed:
            assert observed == evidence
        with pytest.raises(FileExistsError):
            create_private_engine(configured_request, root, outer_socket, image, timeout=10)
        assert json.loads((root / SOURCE_CREATED).read_bytes())["parent_container_id"] == parent_id
    finally:
        if parent_id is None and (root / SOURCE_CREATED).is_file():
            parent_id = json.loads((root / SOURCE_CREATED).read_bytes()).get("parent_container_id")
        if parent_id is not None:
            result = subprocess.run([*outer, "logs", "--tail", "60", parent_id], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print("Disposable DinD parent logs:\n", result.stdout[-4000:], result.stderr[-4000:])
            subprocess.run([*outer, "rm", "--force", parent_id], capture_output=True, text=True, timeout=30, check=True)
        _remove_disposable_engine_root(root)


_PAUSE_DURING_SEALED_API_GATE = """
import json, os, sys, time
from pathlib import Path
from auto_coder.management import fresh_new_host_start as host
original = host._start_docker_container_once
def pause_after_sealing(*args, **kwargs):
    print('private-api-gate-sealed', flush=True)
    release = Path(sys.argv[6])
    deadline = time.monotonic() + 30
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not release.exists():
        raise RuntimeError('private API gate barrier was not released')
    return original(*args, **kwargs)
host._start_docker_container_once = pause_after_sealing
print(json.dumps(host.start_new_docker_once(Path(sys.argv[1]), sys.argv[2], sys.argv[3], private_root=Path(sys.argv[4]), outer_socket=Path(sys.argv[5]))), flush=True)
"""


_HOLD_PRECONNECTED_PRIVATE_API_CLIENT = """
import sys, time
from pathlib import Path
from auto_coder.management.fresh_new_host_start import _LocalDockerConnection
client = _LocalDockerConnection(Path(sys.argv[1]))
client.request('GET', '/v1.41/info')
response = client.getresponse()
assert response.status == 200
response.read()
assert client.sock is not None
print('private-api-client-preconnected', flush=True)
release = Path(sys.argv[2])
deadline = time.monotonic() + 90
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
client.close()
"""


_PAUSE_BEFORE_PRIVATE_SOCKET_SEAL = """
import json, sys, time
from contextlib import contextmanager
from pathlib import Path
from auto_coder.management import fresh_new_host_start as host
from auto_coder.management import fresh_private_engine_gate as gate
original = gate.hold_one_start_api_gate
@contextmanager
def pause_before_seal(*args, **kwargs):
    print('before-private-socket-seal', flush=True)
    release = Path(sys.argv[6])
    deadline = time.monotonic() + 30
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not release.exists():
        raise RuntimeError('private pre-seal barrier was not released')
    with original(*args, **kwargs) as endpoint:
        yield endpoint
gate.hold_one_start_api_gate = pause_before_seal
print(json.dumps(host.start_new_docker_once(Path(sys.argv[1]), sys.argv[2], sys.argv[3], private_root=Path(sys.argv[4]), outer_socket=Path(sys.argv[5]))), flush=True)
"""


@pytest.mark.parametrize(
    "race",
    [
        "none",
        "pre-gate-sibling",
        "pre-gate-live-sibling",
        "preconnected-private-client",
        "pre-seal-start-stop-selected",
        "sealed-gate-outside-client",
        "owner-contention",
        "killed-first-claim",
        "restart-policy-after-claim",
        "changed-bootstrap",
        "changed-executable",
        "changed-application",
        "different-os-uid",
        "wrong-pid1-command",
        "revoked-grant",
        "stale-observation",
    ],
)
def test_private_source_first_launch_and_retained_grant_readback(request_file: Path, race: str) -> None:
    """Cross the private source, host challenge, and real OAuth child boundary."""
    token, request = _prepare_real_new_source(request_file)
    if race in {"revoked-grant", "stale-observation"}:
        bootstrap_path = Path(str(request["bootstrap_config"]))
        bootstrap = json.loads(bootstrap_path.read_bytes())
        bootstrap["verification_timeout"] = 20
        bootstrap_path.write_text(json.dumps(bootstrap))
    if race == "revoked-grant":
        with closing(sqlite3.connect(request_file.parent / "oauth/oauth.sqlite3")) as connection:
            connection.execute("UPDATE grants SET revoked=? WHERE client_id=?", (time.time(), "new-client"))
            connection.commit()
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    original_oauth = (request_file.parent / "oauth/oauth.sqlite3").read_bytes()
    begin_new_instance(request_file)
    outer_socket = Path("/var/run/docker.sock")
    outer = ["docker", "--host", f"unix://{outer_socket}"]
    parent_image = subprocess.run(
        [*outer, "image", "inspect", "docker:29-dind", "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    root = Path(tempfile.mkdtemp(prefix="acpe-", dir="/tmp"))
    parent_id: str | None = None
    child_id = ""
    child_image_tag = ""
    selected_host = f"unix://{root / 'socket/docker.sock'}"
    original_docker_host = os.environ.get("DOCKER_HOST")
    try:
        source = create_private_engine(request_file, root, outer_socket, parent_image, timeout=90)
        parent_id = source.parent_container_id
        base_image = os.environ["AUTOCODER_FRESH_TEST_BASE_IMAGE"]
        save = subprocess.Popen([*outer, "image", "save", base_image], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert save.stdout is not None
        loaded = subprocess.run(
            ["docker", "--host", selected_host, "image", "load"],
            stdin=save.stdout,
            capture_output=True,
            text=True,
            timeout=120,
        )
        save.stdout.close()
        saved_stderr = save.stderr.read() if save.stderr is not None else b""
        assert save.wait(timeout=30) == 0, saved_stderr
        assert loaded.returncode == 0, loaded.stderr
        os.environ["DOCKER_HOST"] = selected_host

        child_id, image_id, child_image_tag = _create_real_new_container(request_file.parent, request_file, request, wrong_command=race == "wrong-pid1-command")
        child_scope = json.loads(subprocess.check_output(["docker", "--host", selected_host, "inspect", child_id], text=True))[0]
        assert child_scope["HostConfig"]["NetworkMode"] == "none"
        assert not child_scope["HostConfig"]["PortBindings"]
        inspected = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", child_id, "--image-id", image_id)
        assert inspected.returncode == 0, inspected.stderr
        arguments = (
            "start-new-container",
            "--request-file",
            str(request_file),
            "--container-id",
            child_id,
            "--image-id",
            image_id,
            "--private-root",
            str(root),
            "--outer-socket",
            str(outer_socket),
        )
        if race == "wrong-pid1-command":
            refused = _run_host(*arguments, timeout=30)
            assert refused.returncode == 1 and refused.stdout == "", refused.stderr
            assert "not the selected Supervisor first start" in refused.stderr
            assert not (request_file.parent / "guard/new-docker-first-start.json").exists()
            assert not (request_file.parent / "state/physical-child-invocations").exists()
            assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
            assert (
                subprocess.check_output(
                    ["docker", "--host", selected_host, "inspect", "--format", "{{.State.Status}}", child_id],
                    text=True,
                ).strip()
                == "created"
            )
            return
        if race in {"changed-bootstrap", "changed-executable", "changed-application"}:
            selected = {
                "changed-bootstrap": Path(str(request["bootstrap_config"])),
                "changed-executable": Path(str(request["executable"])),
                "changed-application": Path(str(request["application_root"])) / "__init__.py",
            }[race]
            selected.write_bytes(selected.read_bytes() + b"\n# modified after protected NEW origin\n")
            refused = _run_host(*arguments, timeout=30)
            assert refused.returncode != 0 and refused.stdout == ""
            assert not (request_file.parent / "guard/new-docker-first-start.json").exists()
            assert not (request_file.parent / "state/physical-child-invocations").exists()
            assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
            assert subprocess.check_output(["docker", "--host", selected_host, "inspect", "--format", "{{.State.Status}}", child_id], text=True).strip() == "created"
            return
        if race == "different-os-uid":
            original_intent = (request_file.parent / "guard/new-instance-origin.json").read_bytes()
            nonowner_image = "auto-coder-new-nonowner:" + source.request_id.replace("-", "")
            with tempfile.TemporaryDirectory(prefix="ac-new-different-uid-") as directory:
                build_root = Path(directory)
                shutil.copytree(Path(str(request["application_root"])), build_root / "auto_coder")
                (build_root / "Dockerfile").write_text(f"FROM {os.environ['AUTOCODER_FRESH_TEST_BASE_IMAGE']}\n" "COPY auto_coder /opt/new-uid-src/auto_coder\n")
                subprocess.run(
                    [*outer, "build", "--network", "none", "-t", nonowner_image, str(build_root)],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            try:
                code = (
                    "import os,runpy,sys;"
                    "assert os.geteuid()==65534;"
                    "sys.stderr.write('verified-nonowner-euid=65534\\n');"
                    f"sys.argv=['fresh_host','start-new-container','--request-file',{str(request_file)!r},"
                    f"'--container-id',{child_id!r},'--image-id',{image_id!r},"
                    f"'--private-root',{str(root)!r},'--outer-socket',{str(outer_socket)!r}];"
                    "runpy.run_module('auto_coder.management.fresh_host',run_name='__main__')"
                )
                refused = subprocess.run(
                    [
                        *outer,
                        "run",
                        "--rm",
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
                        f"type=bind,src={request_file.parent},dst={request_file.parent},readonly",
                        "--mount",
                        f"type=bind,src={root},dst={root},readonly",
                        "--entrypoint",
                        "python3",
                        nonowner_image,
                        "-B",
                        "-c",
                        code,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=40,
                )
                assert refused.returncode == 1 and refused.stdout == "", refused.stderr
                assert "verified-nonowner-euid=65534" in refused.stderr
                assert "Permission denied" in refused.stderr or "owned by effective UID" in refused.stderr
                assert not (request_file.parent / "guard/new-docker-first-start.json").exists()
                assert not (request_file.parent / "state/physical-child-invocations").exists()
                assert (request_file.parent / "guard/new-instance-origin.json").read_bytes() == original_intent
                assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
                assert (
                    subprocess.check_output(
                        ["docker", "--host", selected_host, "inspect", "--format", "{{.State.Status}}", child_id],
                        text=True,
                    ).strip()
                    == "created"
                )
            finally:
                subprocess.run([*outer, "image", "rm", nonowner_image], capture_output=True, timeout=30)
            return
        if race == "preconnected-private-client":
            release = request_file.parent / "release-independent-api-client"
            client = subprocess.Popen(
                [sys.executable, "-B", "-c", _HOLD_PRECONNECTED_PRIVATE_API_CLIENT, str(root / "socket/docker.sock"), str(release)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_environment(),
            )
            try:
                assert client.stdout is not None
                assert select.select([client.stdout], [], [], 30)[0], "independent private Engine client did not connect"
                assert client.stdout.readline() == "private-api-client-preconnected\n"
                refused = _run_host(*arguments, timeout=90)
                assert refused.returncode == 1 and refused.stdout == "", refused.stderr
                assert "another preconnected or unverified API client" in refused.stderr
                claim = (request_file.parent / "guard/new-docker-first-start.json").read_bytes()
                assert not (request_file.parent / "state/physical-child-invocations").exists()
                assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
                assert subprocess.check_output(["docker", "--host", selected_host, "inspect", "--format", "{{.State.Status}}", child_id], text=True).strip() == "created"
            finally:
                release.touch(mode=0o600)
                if client.poll() is None:
                    stdout, stderr = client.communicate(timeout=10)
                    assert client.returncode == 0, (stdout, stderr)
            replay = _run_host(*arguments, timeout=20)
            assert replay.returncode != 0 and replay.stdout == ""
            assert (request_file.parent / "guard/new-docker-first-start.json").read_bytes() == claim
            return
        if race == "pre-seal-start-stop-selected":
            release = request_file.parent / "release-pre-seal"
            suspended = subprocess.Popen(
                [sys.executable, "-B", "-c", _PAUSE_BEFORE_PRIVATE_SOCKET_SEAL, str(request_file), child_id, image_id, str(root), str(outer_socket), str(release)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_environment(),
            )
            try:
                assert suspended.stdout is not None
                assert select.select([suspended.stdout], [], [], 35)[0], "host did not reach pre-seal barrier"
                assert suspended.stdout.readline() == "before-private-socket-seal\n"
                claim = (request_file.parent / "guard/new-docker-first-start.json").read_bytes()
                subprocess.run(["docker", "--host", selected_host, "start", child_id], check=True, capture_output=True, timeout=30)
                subprocess.run(["docker", "--host", selected_host, "stop", "--time", "0", child_id], check=True, capture_output=True, timeout=30)
                assert subprocess.check_output(["docker", "--host", selected_host, "inspect", "--format", "{{.State.Status}}", child_id], text=True).strip() == "exited"
                release.touch(mode=0o600)
                stdout, stderr = suspended.communicate(timeout=60)
                assert suspended.returncode != 0 and stdout == "", (stdout, stderr)
                assert "already started before its original guarded effect" in stderr, stderr
                assert (request_file.parent / "guard/new-docker-first-start.json").read_bytes() == claim
                assert not (request_file.parent / "state/physical-child-invocations").exists()
                assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
            finally:
                release.touch(mode=0o600)
                if suspended.poll() is None:
                    suspended.kill()
                    suspended.communicate(timeout=10)
            assert _run_host(*arguments, timeout=20).returncode != 0
            return
        if race in {"pre-gate-sibling", "pre-gate-live-sibling"}:
            # A second trusted Docker client can reach the socket while the
            # original host holds the guard. The guard-to-effect recheck must
            # refuse any later configured sibling and permanently consume
            # the original one-shot claim, even when that sibling is removed.
            suspended = subprocess.Popen(
                [sys.executable, "-B", "-c", PAUSE_AFTER_HOST_CLAIM, str(request_file), child_id, image_id, str(root), str(outer_socket)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_environment(),
            )
            try:
                assert suspended.stdout is not None
                assert select.select([suspended.stdout], [], [], 30)[0], "private host did not reach claim barrier"
                barrier = suspended.stdout.readline()
                assert barrier == "new-host-claim-fsynced\n", (barrier, suspended.poll(), suspended.stderr.read() if suspended.poll() is not None and suspended.stderr is not None else "")
                original_claim = (request_file.parent / "guard/new-docker-first-start.json").read_bytes()
                assert not (request_file.parent / "state/physical-child-invocations").exists()
                sibling_command = ["--entrypoint", "python3", image_id, "-B", "-c", "import time; time.sleep(180)"] if race == "pre-gate-live-sibling" else ["--entrypoint", "/bin/sh", image_id, "-c", "true"]
                extra = subprocess.run(
                    ["docker", "--host", selected_host, "container", "create", *sibling_command],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout.strip()
                try:
                    if race == "pre-gate-live-sibling":
                        subprocess.run(
                            ["docker", "--host", selected_host, "start", extra],
                            check=True,
                            capture_output=True,
                            timeout=30,
                        )
                        assert (
                            subprocess.check_output(
                                ["docker", "--host", selected_host, "inspect", "--format", "{{.State.Running}}", extra],
                                text=True,
                            ).strip()
                            == "true"
                        )
                    os.kill(suspended.pid, signal.SIGCONT)
                    stdout, stderr = suspended.communicate(timeout=60)
                    assert suspended.returncode == 1 and stdout == "", (suspended.returncode, stdout, stderr)
                    assert "another configured startup source" in stderr, stderr
                    assert (request_file.parent / "guard/new-docker-first-start.json").read_bytes() == original_claim
                    assert not (request_file.parent / "state/physical-child-invocations").exists()
                    assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
                finally:
                    subprocess.run(
                        ["docker", "--host", selected_host, "container", "rm", "--force", extra],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                replay = _run_host(*arguments, timeout=15)
                assert replay.returncode != 0 and replay.stdout == ""
                assert (request_file.parent / "guard/new-docker-first-start.json").read_bytes() == original_claim
                assert not (request_file.parent / "state/physical-child-invocations").exists()
                return
            finally:
                if suspended.poll() is None:
                    suspended.kill()
                    suspended.communicate(timeout=10)
        if race in {"owner-contention", "killed-first-claim", "restart-policy-after-claim"}:
            suspended = subprocess.Popen(
                [sys.executable, "-B", "-c", PAUSE_AFTER_HOST_CLAIM, str(request_file), child_id, image_id, str(root), str(outer_socket)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_environment(),
            )
            try:
                assert suspended.stdout is not None
                assert select.select([suspended.stdout], [], [], 30)[0]
                assert suspended.stdout.readline() == "new-host-claim-fsynced\n"
                claim_bytes = (request_file.parent / "guard/new-docker-first-start.json").read_bytes()
                assert not (request_file.parent / "state/physical-child-invocations").exists()
                if race == "killed-first-claim":
                    os.kill(suspended.pid, signal.SIGKILL)
                    assert suspended.wait(timeout=10) == -signal.SIGKILL
                    replay = _run_host(*arguments, timeout=20)
                    assert replay.returncode != 0 and replay.stdout == ""
                    assert (request_file.parent / "guard/new-docker-first-start.json").read_bytes() == claim_bytes
                    assert not (request_file.parent / "state/physical-child-invocations").exists()
                    assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
                    return
                if race == "restart-policy-after-claim":
                    subprocess.run(
                        ["docker", "--host", selected_host, "update", "--restart=always", child_id],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    os.kill(suspended.pid, signal.SIGCONT)
                    stdout, stderr = suspended.communicate(timeout=60)
                    assert suspended.returncode == 1 and stdout == "", (stdout, stderr)
                    assert "restart policy" in stderr or "startup source" in stderr, stderr
                    assert (request_file.parent / "guard/new-docker-first-start.json").read_bytes() == claim_bytes
                    assert not (request_file.parent / "state/physical-child-invocations").exists()
                    assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
                    subprocess.run(
                        ["docker", "--host", selected_host, "update", "--restart=no", child_id],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    assert _run_host(*arguments, timeout=20).returncode != 0
                    return
                loser = _run_host(*arguments, timeout=20)
                assert loser.returncode != 0 and loser.stdout == ""
                assert (request_file.parent / "guard/new-docker-first-start.json").read_bytes() == claim_bytes
                os.kill(suspended.pid, signal.SIGCONT)
                stdout, stderr = suspended.communicate(timeout=150)
                assert suspended.returncode == 0, stderr
                launch = subprocess.CompletedProcess(arguments, suspended.returncode, stdout, stderr)
            finally:
                if suspended.poll() is None:
                    suspended.kill()
                    suspended.communicate(timeout=10)
        elif race == "sealed-gate-outside-client":
            release = request_file.parent / "release-original-gate"
            suspended = subprocess.Popen(
                [sys.executable, "-B", "-c", _PAUSE_DURING_SEALED_API_GATE, str(request_file), child_id, image_id, str(root), str(outer_socket), str(release)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_environment(),
            )
            try:
                assert suspended.stdout is not None
                assert select.select([suspended.stdout], [], [], 35)[0], "private API gate never became active"
                assert suspended.stdout.readline() == "private-api-gate-sealed\n"
                assert (root / "socket/docker.sock").stat().st_mode & 0o777 == 0
                gate_host = f"unix://{root / 'socket/first-start-gate.sock'}"
                # Independent Docker CLI sources cannot mutate either the
                # original raw socket or the read-only proxy during the gap.
                blocked_raw = subprocess.run(
                    ["docker", "--host", selected_host, "container", "create", "--entrypoint", "/bin/sh", image_id, "-c", "true"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert blocked_raw.returncode != 0, blocked_raw.stdout
                blocked_gateway = subprocess.run(
                    ["docker", "--host", gate_host, "container", "create", "--entrypoint", "/bin/sh", image_id, "-c", "true"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert blocked_gateway.returncode != 0, blocked_gateway.stdout
                assert not (request_file.parent / "state/physical-child-invocations").exists()
                assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
                release.touch(mode=0o600)
                stdout, stderr = suspended.communicate(timeout=150)
                assert suspended.returncode == 0, stderr
                launch = subprocess.CompletedProcess(arguments, suspended.returncode, stdout, stderr)
            finally:
                release.touch(mode=0o600)
                if suspended.poll() is None:
                    suspended.kill()
                    suspended.communicate(timeout=10)
        else:
            launch = _run_host(*arguments, timeout=150)
        assert launch.returncode == 0, (launch.stderr, subprocess.run(["docker", "logs", child_id], capture_output=True, text=True).stdout)
        assert json.loads(launch.stdout)["status"] == "first-child-observed-awaiting-authenticated-readback"
        assert stat.S_IMODE((root / "socket/docker.sock").stat().st_mode) == 0o600
        assert not (root / "socket/first-start-gate.sock").exists()
        assert subprocess.check_output(["docker", "--host", selected_host, "ps", "--all", "--quiet", "--no-trunc"], text=True).splitlines() == [child_id]
        claim = json.loads((request_file.parent / "guard/new-docker-first-start.json").read_bytes())
        assert claim["profile"] == "genuinely-new-private-engine-v1"
        assert claim["private_source"]["parent_container_id"] == source.parent_container_id
        assert claim["private_source"]["private_engine_id"] == source.private_engine_id
        assert len((request_file.parent / "state/physical-child-invocations").read_text().splitlines()) == 1
        replay = _run_host(*arguments, timeout=15)
        assert replay.returncode != 0 and replay.stdout == ""

        observation_id = json.loads((request_file.parent / "state/new-first-launched.json").read_bytes())["observation_id"]
        challenge_deadline = time.monotonic() + 20
        while not _original_private_child_requires_bearer(child_id):
            if time.monotonic() >= challenge_deadline:
                raise AssertionError("private NEW child never produced the configured unauthenticated Bearer challenge")
            time.sleep(0.25)
        if race in {"revoked-grant", "stale-observation"}:
            tested_id = "0" * 32 if race == "stale-observation" else observation_id
            expected_status = 401 if race == "revoked-grant" else 200
            deadline = time.monotonic() + 18
            while time.monotonic() < deadline:
                refused = _authorized_readback(child_id, token, tested_id, expected_status=expected_status)
                if refused.returncode == 0:
                    break
                time.sleep(0.3)
            else:
                raise AssertionError("private retained-grant negative readback did not reach the real child: " + refused.stderr[-1200:])
            assert _wait_for_new_outcome(request_file.parent, timeout=35)["status"] == "unknown"
            assert len((request_file.parent / "state/physical-child-invocations").read_text().splitlines()) == 1
            assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
            assert _run_host(*arguments, timeout=15).returncode != 0
            return
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            readback = _authorized_readback(child_id, token, observation_id)
            if readback.returncode == 0:
                break
            time.sleep(0.3)
        else:
            raise AssertionError("NEW private source never produced fresh authorized readback: " + readback.stderr[-1200:])
        assert _wait_for_new_outcome(request_file.parent)["status"] == "succeeded"
        assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        assert len((request_file.parent / "state/physical-child-invocations").read_text().splitlines()) == 1
        # A second CREATED child must poison this same source's live exclusion.
        # Never erase a prior child and then assert a fresh first-launch.
        extra = subprocess.run(
            ["docker", "--host", selected_host, "container", "create", "--entrypoint", "/bin/sh", image_id, "-c", "true"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        try:
            with pytest.raises(HostObservationUnavailable, match="another configured startup source"):
                assert_private_engine_live(request_file, root, outer_socket, source, container_id=child_id)
            assert len((request_file.parent / "state/physical-child-invocations").read_text().splitlines()) == 1
            assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        finally:
            subprocess.run(
                ["docker", "--host", selected_host, "container", "rm", extra],
                check=True,
                capture_output=True,
                timeout=30,
            )
    finally:
        if original_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = original_docker_host
        if child_id:
            subprocess.run(["docker", "--host", selected_host, "rm", "--force", child_id], capture_output=True, timeout=30)
        if child_image_tag:
            subprocess.run(["docker", "--host", selected_host, "image", "rm", child_image_tag], capture_output=True, timeout=30)
        if parent_id is None and (root / SOURCE_CREATED).is_file():
            parent_id = json.loads((root / SOURCE_CREATED).read_bytes()).get("parent_container_id")
        if parent_id is not None:
            logs = subprocess.run([*outer, "logs", "--tail", "80", parent_id], capture_output=True, text=True, timeout=10)
            if logs.returncode == 0:
                print("Disposable DinD parent logs:\n", logs.stdout[-4000:], logs.stderr[-4000:])
            subprocess.run([*outer, "rm", "--force", parent_id], capture_output=True, text=True, timeout=30, check=True)
        _remove_disposable_engine_root(root)
