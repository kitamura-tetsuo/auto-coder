"""Component controls for fixture networking and observation retries; no Docker."""

import copy
import errno
import json
import os
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_management_archived_cutover_container import Chain, diagnostic_log_lines, publish_diagnostics, transport_failure, worker_compose_profile


def test_explicit_gateway_preserves_canonical_worker_isolation():
    fixtures = Path(__file__).parent / "fixtures"
    original = yaml.safe_load((fixtures / "compose.supervisor-network.yml").read_text())
    profile = worker_compose_profile(fixtures, "10.252.199.0/24", "10.252.199.1")
    assert profile["networks"]["private"]["internal"] is True
    assert profile["networks"]["private"]["ipam"]["config"] == [{"subnet": "10.252.199.0/24", "gateway": "10.252.199.1"}]
    without_ipam = copy.deepcopy(profile)
    without_ipam["networks"]["private"]["ipam"] = original["networks"]["private"]["ipam"]
    assert without_ipam == original
    for invalid in ("10.253.199.1", "10.252.199.0", "10.252.199.11", "10.252.199.12", "10.252.199.255"):
        with pytest.raises(ValueError):
            worker_compose_profile(fixtures, "10.252.199.0/24", invalid)


def test_connection_reset_never_counts_as_readiness(monkeypatch):
    fixture = Chain.__new__(Chain)
    fixture.worker_baseline = None
    fixture.ensure_proxy = lambda _: None
    events = []

    def call(generation, tool, arguments, *, authenticated=True):
        events.append(authenticated)
        if len(events) == 1:
            raise ConnectionResetError("disposable startup reset")
        return {"uid": "operator"} if authenticated else {"http_status": 401}

    fixture.call = call
    monkeypatch.setattr("tests.test_management_archived_cutover_container.time.sleep", lambda _: None)
    fixture.ready(SimpleNamespace(), fresh=False)
    assert events == [False, False, True]


def test_bounded_diagnostics_drop_headers_bodies_and_redact_credentials():
    import json

    token = "synthetic-credential-" + "a" * 40
    raw = json.dumps({"level": "error", "msg": "upstream unavailable", "request": {"headers": {"Authorization": ["Bearer " + token]}, "body": token}, "error": "dial refused"})
    lines = diagnostic_log_lines(raw + "\nRuntimeError: Bearer " + token, token)
    assert len(lines) == 2
    assert token not in "".join(lines)
    assert "headers" not in "".join(lines) and "body" not in "".join(lines)
    assert "upstream unavailable" in lines[0] and "dial refused" in lines[0]


def test_child_diagnostics_never_create_a_missing_ledger(tmp_path):
    import sqlite3

    generation = SimpleNamespace(path=lambda _: tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        Chain.child(generation)
    assert not (tmp_path / "management-child.sqlite3").exists()


def test_diagnostics_reject_nested_and_plaintext_headers_and_bodies():
    import json

    samples = [
        json.dumps({"level": "error", "error": {"headers": {"Authorization": "Basic eA=="}, "body": "abc"}}),
        json.dumps({"level": "error", "msg": {"body": "abc"}}),
        "RuntimeError: headers={'Authorization':'Basic eA=='} body=abc",
        json.dumps({"level": "error", "msg": "response body: abc"}),
    ]
    for sample in samples:
        result = "".join(diagnostic_log_lines(sample))
        assert "headers" not in result.lower() and "body" not in result.lower()
        assert "eA==" not in result and "abc" not in result


def test_transport_diagnostic_keeps_errno_without_server_text():
    error = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "private arbitrary diagnostic text"))
    assert transport_failure(error) == "URLError:ConnectionRefusedError errno=" + str(errno.ECONNREFUSED)
    assert transport_failure(urllib.error.URLError("private URL")) == "URLError:str"


def test_sanitized_artifact_keeps_private_mode_and_matches_uploader_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)
    original = tmp_path / "protected-state"
    original.mkdir(mode=0o700)
    before = original.stat()
    artifact = tmp_path / "sanitized"
    publish_diagnostics(artifact, {"status": "refused"})
    output = artifact / "observations.json"
    parent = tmp_path.stat()
    assert (artifact.stat().st_uid, artifact.stat().st_gid) == (parent.st_uid, parent.st_gid)
    assert (output.stat().st_uid, output.stat().st_gid) == (parent.st_uid, parent.st_gid)
    assert artifact.stat().st_mode & 0o777 == 0o700 and output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {"status": "refused"}
    assert original.stat() == before
    assert os.access(output, os.R_OK)
    with pytest.raises(FileExistsError):
        publish_diagnostics(artifact, {"status": "overwritten"})


def test_loopback_http_challenge_ignores_proxy_environment(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers.get("Authorization") is None
            Handler.calls += 1
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        fixture = Chain.__new__(Chain)
        fixture.port = server.server_port
        assert fixture.call(SimpleNamespace(proxy="fixture"), "auto_coder_whoami", {}, authenticated=False) == {"http_status": 401}
        assert Handler.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_management_ingress_keeps_workers_on_the_internal_network(tmp_path):
    fixture = Chain.__new__(Chain)
    fixture.root = tmp_path
    fixture.oauth = tmp_path / "oauth"
    fixture.oauth.mkdir()
    fixture.origin = "http://127.0.0.1:8081"
    fixture.executable_digest = "f" * 64
    fixture.image = "sha256:" + "e" * 64
    fixture.port = 23456
    fixture.worker_network = "private-workers"
    fixture.worker_environment = {"SUPERVISOR_BETA_IP": "10.252.99.12", "SUPERVISOR_RELEASE_IP": "10.252.99.11"}
    fixture.names, fixture.generations = [], []
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "create":
            return "a" * 64
        if args[:2] == ("network", "connect"):
            return ""
        if args[0] == "inspect":
            return json.dumps({"bridge": {}, "private-workers": {}})
        pytest.fail("unexpected fixture control")

    fixture.docker = docker
    generation = fixture.prepare("manager-unit")
    create = calls[0]
    assert create[create.index("--network") + 1] == "bridge"
    assert create[create.index("--publish") + 1] == "127.0.0.1:23456:8080"
    assert calls[1] == ("network", "connect", "private-workers", generation.container)
    assert len(calls) == 3
