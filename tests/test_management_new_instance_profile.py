"""Separate-process semantic NEW profile checks, with retained SQLite authority."""

import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from auto_coder.management.action_scopes import READ_SCOPE
from auto_coder.management.bootstrap_store import BootstrapStore
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_store import OAuthStore
from tests.test_management_new_instance_preflight import _bytes, _environment, request_file

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def _use_real_commands() -> None:
    pass


@pytest.fixture
def _use_real_sleep() -> None:
    pass


@pytest.fixture
def configured_request(request_file: Path) -> Path:
    request = json.loads(request_file.read_text())
    root = request_file.parent
    origin = "http://127.0.0.1:18881"
    oauth_settings = OAuthSettings(Path(request["oauth_root"]) / "oauth.sqlite3", origin, "fresh-project", (OAuthClient("client", (origin + "/callback",)),))
    OAuthStore(oauth_settings)
    config = {
        "instance_id": request["new_instance"],
        "state_root": request["state_root"],
        "journal_root": request["operation_root"],
        "oauth_root": request["oauth_root"],
        "listener_url": origin + "/mcp",
        "initial_runtime": request["runtime_reference"],
        "runtimes": [
            {
                "reference": request["runtime_reference"],
                "receipt": "sha256:" + hashlib.sha256(Path(request["executable"]).read_bytes()).hexdigest(),
                "executable": request["executable"],
                "arguments": ["management", "serve", "--config", request["management_config"]],
                "source_root": request["application_root"],
                "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
            }
        ],
        "fresh_guard_path": request["guard_root"] + "/fresh-guard.sock",
        "new_instance_initialization": True,
    }
    Path(request["bootstrap_config"]).write_text(json.dumps(config))
    Path(request["management_config"]).write_text(
        yaml.safe_dump(
            {
                "state_root": request["oauth_root"],
                "operation_state_root": request["operation_root"],
                "bootstrap_config_path": request["bootstrap_config"],
                "public_origin": origin,
                "firebase_project_id": "fresh-project",
                "clients": [{"client_id": "client", "redirect_uris": [origin + "/callback"], "allowed_scopes": [READ_SCOPE]}],
                "enabled_scopes": [READ_SCOPE],
                "bind_host": "127.0.0.1",
                "port": 18881,
                "local_development": True,
            }
        )
    )
    return request_file


def _run(command: str, request: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.management.fresh_host", command, "--request-file", str(request)],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_independent_profile_matches_selected_config_and_retained_oauth_without_launch(configured_request: Path) -> None:
    root = configured_request.parent
    original = _bytes(root)
    begin = _run("begin", configured_request)
    assert begin.returncode == 0, begin.stderr
    result = _run("inspect-profile", configured_request)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {
        "request_id": json.loads(configured_request.read_text())["request_id"],
        "origin_sha256": json.loads(begin.stdout)["record_sha256"],
        "instance_id": "fixture-new",
        "runtime_reference": "fixture-runtime",
        "listener_url": "http://127.0.0.1:18881/mcp",
        "oauth_project": "fresh-project",
        "status": "profile-verified-only",
        "launch_authorized": False,
    }
    assert {path: content for path, content in _bytes(root).items() if not path.startswith("guard/")} == original
    assert all(list((root / name).iterdir()) == [] for name in ("state", "operations"))
    assert not (root / "LAUNCHED").exists()


@pytest.mark.parametrize("fault", ["", "wrong-host-port", "wildcard-without-network", "network-with-loopback-bind", "invalid-network-name"])
def test_explicit_new_docker_loopback_profile_is_bound_to_original_files(configured_request: Path, fault: str) -> None:
    request = json.loads(configured_request.read_bytes())
    root = configured_request.parent
    bootstrap_path = Path(request["bootstrap_config"])
    management_path = Path(request["management_config"])
    bootstrap = json.loads(bootstrap_path.read_bytes())
    management = yaml.safe_load(management_path.read_bytes())
    if fault != "wildcard-without-network":
        bootstrap["new_docker_network"] = "ac-new-0123456789abcdef"
        bootstrap["new_docker_host_port"] = 18882 if fault == "wrong-host-port" else 18881
        if fault == "invalid-network-name":
            bootstrap["new_docker_network"] = "bridge"
    if fault != "network-with-loopback-bind":
        management["bind_host"] = "0.0.0.0"
    bootstrap_path.write_text(json.dumps(bootstrap))
    management_path.write_text(yaml.safe_dump(management))
    preserved = _bytes(root)
    begin = _run("begin", configured_request)
    assert begin.returncode == 0, begin.stderr
    verified = _run("inspect-profile", configured_request)
    if fault:
        assert verified.returncode == 1 and verified.stdout == "", (verified.stdout, verified.stderr)
    else:
        assert verified.returncode == 0, verified.stderr
        assert json.loads(verified.stdout)["launch_authorized"] is False
    assert {path: content for path, content in _bytes(root).items() if not path.startswith("guard/")} == preserved
    assert list((root / "state").iterdir()) == []
    assert list((root / "operations").iterdir()) == []


@pytest.mark.parametrize("owner_evidence", ["config-only", "stale-owner", "mismatched-owner"])
def test_host_management_serve_refuses_new_docker_wildcard_without_live_pid1_owner(configured_request: Path, owner_evidence: str) -> None:
    """The real serve CLI must not turn a NEW YAML selector into host-wide HTTP."""
    request = json.loads(configured_request.read_bytes())
    root = configured_request.parent
    bootstrap_path = Path(request["bootstrap_config"])
    management_path = Path(request["management_config"])
    bootstrap = json.loads(bootstrap_path.read_bytes())
    management = yaml.safe_load(management_path.read_bytes())

    # An unused host port makes any illicit new wildcard socket observable.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as observer:
        observer.bind(("127.0.0.1", 0))
        port = observer.getsockname()[1]
    bootstrap["new_docker_network"] = "ac-new-0123456789abcdef"
    bootstrap["new_docker_host_port"] = port
    management["public_origin"] = f"http://127.0.0.1:{port}"
    management["clients"][0]["redirect_uris"] = [f"http://127.0.0.1:{port}/callback"]
    management["bind_host"] = "0.0.0.0"
    bootstrap_path.write_text(json.dumps(bootstrap))
    management_path.write_text(yaml.safe_dump(management))
    assert yaml.safe_load(management_path.read_bytes())["bind_host"] == "0.0.0.0"
    oauth_before = {path.name: path.read_bytes() for path in (root / "oauth").iterdir() if path.is_file()}

    if owner_evidence != "config-only":
        owner_lock = root / "state/.supervisor-owner.lock"
        owner_lock.touch(mode=0o600)
        owner_lock.chmod(0o600)
        ledger = BootstrapStore(Path(request["state_root"]), str(request["new_instance"]))
        ledger.publish_supervisor("unadmitted-stale-owner" if owner_evidence == "stale-owner" else "mismatched-owner", 1, "123456")

    def wildcard_listeners() -> set[str]:
        return {line.split()[1] for line in Path("/proc/net/tcp").read_text().splitlines()[1:] if line.split()[3] == "0A" and line.split()[1] == f"00000000:{port:04X}"}

    before = wildcard_listeners()
    command = [sys.executable, "-B", "-m", "auto_coder.cli", "management", "serve", "--config", str(management_path)]
    process = subprocess.Popen(command, env=_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        observed = set()
        deadline = time.monotonic() + 8
        while process.poll() is None and time.monotonic() < deadline:
            observed.update(wildcard_listeners())
            time.sleep(0.01)
        assert process.poll() is not None, "unadmitted host management serve remained running"
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert process.returncode != 0 and stdout == "", (stdout, stderr)
    assert "NEW Docker HTTP child must be directly owned" in stderr, stderr
    assert not observed - before and wildcard_listeners() == before
    assert {path.name: path.read_bytes() for path in (root / "oauth").iterdir() if path.is_file()} == oauth_before
    assert not (root / "LAUNCHED").exists()


@pytest.mark.parametrize(
    "fault",
    (
        "instance",
        "runtime",
        "application",
        "roots",
        "guard",
        "environment",
        "endpoint",
        "management_path",
        "oauth_project",
        "oauth_read_scope",
        "oauth_database_missing",
        "oauth_schema",
        "selector_missing",
        "selector_not_boolean",
        "prior_lock",
    ),
)
def test_profile_mismatch_refuses_without_touching_original_state(configured_request: Path, fault: str) -> None:
    root = configured_request.parent
    request = json.loads(configured_request.read_text())
    bootstrap_path = Path(request["bootstrap_config"])
    management_path = Path(request["management_config"])
    bootstrap = json.loads(bootstrap_path.read_text())
    management = yaml.safe_load(management_path.read_text())
    if fault == "instance":
        bootstrap["instance_id"] = "another-instance"
    elif fault == "runtime":
        bootstrap["initial_runtime"] = "missing-reference"
    elif fault == "application":
        bootstrap["runtimes"][0]["source_root"] = str(root / "another-app")
    elif fault == "roots":
        bootstrap["journal_root"] = str(root / "another-operations")
    elif fault == "guard":
        bootstrap["fresh_guard_path"] = str(root / "other-guard/fresh-guard.sock")
    elif fault == "environment":
        bootstrap["runtimes"][0]["environment"]["PYTHONPATH"] = str(root / "other-app")
    elif fault == "endpoint":
        bootstrap["listener_url"] = "http://127.0.0.1:18882/mcp"
    elif fault == "management_path":
        management["bootstrap_config_path"] = str(root / "other-bootstrap.json")
    elif fault == "oauth_project":
        management["firebase_project_id"] = "another-project"
    elif fault == "oauth_read_scope":
        management["enabled_scopes"] = ["auto-coder.operate"]
    elif fault == "selector_missing":
        del bootstrap["new_instance_initialization"]
    elif fault == "selector_not_boolean":
        bootstrap["new_instance_initialization"] = "true"
    elif fault == "prior_lock":
        bootstrap["fresh_prior_lock_path"] = str(root / "invented-prior-owner.lock")
    elif fault == "oauth_database_missing":
        (root / "oauth/oauth.sqlite3").rename(root / "oauth/preserved-original.sqlite3")
    else:
        (root / "oauth/oauth.sqlite3").write_bytes(b"original but malformed database")
    bootstrap_path.write_text(json.dumps(bootstrap))
    management_path.write_text(yaml.safe_dump(management))
    original = _bytes(root)
    assert _run("begin", configured_request).returncode == 0
    refusal = _run("inspect-profile", configured_request)
    assert refusal.returncode == 1
    assert refusal.stdout == ""
    assert "fresh host observation unavailable:" in refusal.stderr
    assert {path: content for path, content in _bytes(root).items() if not path.startswith("guard/")} == original
    assert all(list((root / name).iterdir()) == [] for name in ("state", "operations"))
    assert not (root / "LAUNCHED").exists()


@pytest.mark.parametrize("legacy_flag", [[], ["--fresh-initialization"]])
def test_new_profile_cannot_enter_legacy_bootstrap_without_admission(configured_request: Path, legacy_flag: list[str]) -> None:
    request = json.loads(configured_request.read_text())
    root = configured_request.parent
    before = _bytes(root)
    result = subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.management.bootstrap_supervisor", "--config", request["bootstrap_config"], *legacy_flag],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "NEW initialization requires a separately admitted first-launch controller" in result.stderr
    assert _bytes(root) == before
    assert all(list((root / name).iterdir()) == [] for name in ("state", "operations", "guard"))
    assert not (root / "LAUNCHED").exists()


@pytest.mark.parametrize("changed", ["bootstrap", "management", "application", "corrupt_oauth"])
def test_profile_reverification_refuses_changed_original_evidence(configured_request: Path, changed: str) -> None:
    assert _run("begin", configured_request).returncode == 0
    root = configured_request.parent
    request = json.loads(configured_request.read_text())
    path = {"bootstrap": Path(request["bootstrap_config"]), "management": Path(request["management_config"]), "application": root / "app/main.py", "corrupt_oauth": root / "oauth/oauth.sqlite3"}[changed]
    path.write_bytes(b"unreadable OAuth SQLite header" if changed == "corrupt_oauth" else path.read_bytes() + b"changed after origin")
    before = _bytes(root)
    result = _run("inspect-profile", configured_request)
    assert result.returncode == 1
    assert result.stdout == ""
    assert _bytes(root) == before
    assert not (root / "LAUNCHED").exists()
