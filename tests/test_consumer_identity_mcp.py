"""Installed-file evidence through the authenticated management and worker path."""

import hashlib
import os
import shutil
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import auto_coder.consumer_identity as identity
from auto_coder.management.action_scopes import READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.worker_network_client import WorkerNetworkResult
from tests.test_management_mcp import _issue, real_boundary
from tests.test_management_runtime_mcp import _call, runtime_boundary
from tests.test_worker_supervisor_bridge import _bridge, _supervisor


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
def test_authenticated_mcp_through_live_worker(runtime_boundary, tmp_path, monkeypatch):
    settings, grants, signed, _policy, _beta, release = runtime_boundary
    package = tmp_path / "consumer-package"
    package.mkdir()
    source = Path(identity.__file__).resolve().parent
    for name in identity._FILES:
        shutil.copyfile(source / name, package / name)
    monkeypatch.setenv("AUTO_CODER_TEST_CONSUMER_PACKAGE", str(package))
    supervisor = _supervisor(tmp_path / "live")
    listener, beta = _bridge(supervisor, os.urandom(48))
    try:
        beta.inspect_supervisor()
        started = supervisor.apply({"operation_id": "start-worker", "operation": "START"})
        assert started["status"] == "RUNNING"
        for _ in range(100):
            if beta.observe_worker().result_code == "OK":
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("worker did not expose its observation")
        app = create_management_app(settings, grants, worker_clients={"beta": beta, "release": release})
        with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
            token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
            result = _call(client, token, "auto_coder_consumer_identity", {"target_id": "beta-worker"})
            assert result["status"] == "OK"
            assert result["binding_status"] == "UNAVAILABLE"
            assert result["runtime_binding"] is None
            assert result["repository"] == "fixture/beta"
            assert result["process_run_id"] == beta.worker_process_run_id
            assert result["deployment_generation"] == beta.worker_deployment_generation
            projection = result["projection"]
            assert projection["origin"] == "worker_package_disk_sha256"
            root = Path(identity.__file__).resolve().parent
            expected = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("requirement_contract.py", "specification_analyzer.py", "specification_validation_lifecycle.py", "issue_review_service.py", "prompts.yaml")}
            assert projection["sha256"] == expected
            assert "already-loaded Python code are not attested" in projection["semantics"]
            assert _call(client, token, "auto_coder_consumer_identity", {"target_id": "unknown"})["status"] == "DENIED"
            prompts = package / "prompts.yaml"
            original = prompts.read_bytes()
            prompts.write_bytes(b"different")
            assert _call(client, token, "auto_coder_consumer_identity", {"target_id": "beta-worker"})["status"] == "CONFLICT"
            prompts.unlink()
            assert _call(client, token, "auto_coder_consumer_identity", {"target_id": "beta-worker"})["status"] == "UNAVAILABLE"
            prompts.write_bytes(original)
            beta.worker_process_run_id = "wrong-process"
            assert beta.diagnostics("consumer-identity").result_code == "CONFLICT"
            beta.inspect_supervisor()
            hold = tmp_path / "live" / "fixture-hold-consumer"
            hold.write_text("hold")
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(_call, client, token, "auto_coder_consumer_identity", {"target_id": "beta-worker"})
                for _ in range(100):
                    if (tmp_path / "live" / "fixture-consumer-entered").exists():
                        break
                    threading.Event().wait(0.05)
                else:
                    pytest.fail("worker did not enter the held collection")
                assert supervisor.child is not None
                os.kill(supervisor.child.pid, signal.SIGKILL)
                supervisor.child.wait(timeout=10)
                assert pending.result(timeout=10)["status"] in {"UNAVAILABLE", "CONFLICT", "UNKNOWN"}
    finally:
        if supervisor.child is not None and supervisor.child.poll() is None:
            supervisor.child.terminate()
            (tmp_path / "live" / "fixture-release").write_text("release")
            supervisor.child.wait(timeout=10)
        listener.close()


def test_file_samples_report_change_and_missing(monkeypatch, tmp_path):
    source = Path(identity.__file__).resolve().parent
    for name in identity._FILES:
        shutil.copyfile(source / name, tmp_path / name)
    monkeypatch.setattr(identity, "_PACKAGE", tmp_path)
    observed = identity.ConsumerIdentity()
    assert observed.observe()["status"] == "OK"
    (tmp_path / "prompts.yaml").write_bytes(b"changed")
    assert observed.observe() == {"status": "CONFLICT", "reason": "file_samples_differ"}
    (tmp_path / "prompts.yaml").unlink()
    assert observed.observe() == {"status": "UNAVAILABLE", "reason": "file_measurement_unavailable"}
    (tmp_path / "prompts.yaml").symlink_to(source / "prompts.yaml")
    assert observed.observe()["status"] == "UNAVAILABLE"
    (tmp_path / "prompts.yaml").unlink()
    with (tmp_path / "prompts.yaml").open("wb") as stream:
        stream.truncate(identity._MAX_FILE_BYTES + 1)
    assert observed.observe()["status"] == "UNAVAILABLE"


@pytest.fixture
def sampled_package(monkeypatch, tmp_path):
    source = Path(identity.__file__).resolve().parent
    for name in identity._FILES:
        shutil.copyfile(source / name, tmp_path / name)
    monkeypatch.setattr(identity, "_PACKAGE", tmp_path)
    observed = identity.ConsumerIdentity()
    assert observed.observe()["status"] == "OK"
    return observed, tmp_path


def test_file_change_between_collection_scans_is_conflict(monkeypatch, sampled_package):
    observed, package = sampled_package
    original_measure = identity._measure
    calls = []

    def change_after_first_scan():
        result = original_measure()
        calls.append(result)
        if len(calls) == 1:
            (package / "prompts.yaml").write_bytes(b"changed between scans")
        return result

    monkeypatch.setattr(identity, "_measure", change_after_first_scan)
    assert observed.observe() == {"status": "CONFLICT", "reason": "file_samples_differ"}
    assert len(calls) == 2
    assert calls[0]["prompts.yaml"] != calls[1]["prompts.yaml"]


def test_metadata_change_during_open_file_read_is_unavailable(monkeypatch, sampled_package):
    observed, package = sampled_package
    target = package / "requirement_contract.py"
    original_fdopen = os.fdopen
    changed = []

    class ChangingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            data = self.stream.read(size)
            if data and not changed:
                before = target.stat()
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                changed.append((before.st_mtime_ns, target.stat().st_mtime_ns))
            return data

    monkeypatch.setattr(identity.os, "fdopen", lambda fd, mode: ChangingReader(original_fdopen(fd, mode)))
    assert observed.observe() == {"status": "UNAVAILABLE", "reason": "file_measurement_unavailable"}
    assert len(changed) == 1 and changed[0][0] != changed[0][1]


def test_mcp_rejects_invalid_worker_evidence_and_conflicting_binding(runtime_boundary):
    settings, grants, signed, _policy, beta, release = runtime_boundary
    beta.diagnostics = lambda kind, params: WorkerNetworkResult(200, "OK", {"status": "OK", "sha256": {}}, beta.generation, "diagnostic")
    app = create_management_app(settings, grants, worker_clients={"beta": beta, "release": release})
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
        assert _call(client, token, "auto_coder_consumer_identity", {"target_id": "beta-worker"})["reason"] == "invalid_consumer_identity"
        beta.wrong_lock = True
        assert _call(client, token, "auto_coder_consumer_identity", {"target_id": "beta-worker"}) == {"status": "CONFLICT", "reason": "runtime_binding_conflict"}
