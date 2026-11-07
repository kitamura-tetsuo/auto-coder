"""Component controls for fixture networking and observation retries; no Docker."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_management_archived_cutover_container import Chain, diagnostic_log_lines, worker_compose_profile


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
