import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import Mock

from click.testing import CliRunner

from auto_coder.cli import main


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_management_command_bypasses_worker_and_updater(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "management.yml"
    config.write_text(
        f"""state_root: {tmp_path / 'state'}
public_origin: http://127.0.0.1:8081
firebase_project_id: project
local_development: true
clients:
  - client_id: desktop
    redirect_uris: [http://127.0.0.1:9191/callback]
""",
        encoding="utf-8",
    )
    updater = Mock(side_effect=AssertionError("updater must not run"))
    github = Mock(side_effect=AssertionError("GitHub must not be checked"))
    serve = Mock()
    monkeypatch.setattr("auto_coder.cli.check_for_updates_and_restart", updater)
    monkeypatch.setattr("auto_coder.auth_utils.verify_github_access", github)
    monkeypatch.setattr("uvicorn.run", serve)

    result = CliRunner().invoke(main, ["management", "serve", "--config", str(config)])

    assert result.exit_code == 0, result.output
    updater.assert_not_called()
    github.assert_not_called()
    serve.assert_called_once()
    assert serve.call_args.kwargs["host"] == "127.0.0.1"
    assert serve.call_args.kwargs["port"] == 8081


def test_management_invalid_config_never_serves(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "management.yml"
    config.write_text("public_origin: http://example.test\n", encoding="utf-8")
    serve = Mock()
    monkeypatch.setattr("uvicorn.run", serve)

    result = CliRunner().invoke(main, ["management", "serve", "--config", str(config)])

    assert result.exit_code != 0
    assert "invalid management configuration" in result.output
    serve.assert_not_called()


def test_actual_management_cli_serves_alongside_worker_lock(tmp_path: Path) -> None:
    port = _free_port()
    state = tmp_path / "state"
    state.mkdir()
    (state / "allowed_uids.json").write_text(
        json.dumps({"project_id": "project", "allowed_uids": ["operator"]}),
        encoding="utf-8",
    )
    config = tmp_path / "management.yml"
    config.write_text(
        f"""state_root: {state}
public_origin: http://127.0.0.1:{port}
firebase_project_id: project
bind_host: 127.0.0.1
port: {port}
local_development: true
clients:
  - client_id: desktop
    redirect_uris: [http://127.0.0.1:9191/callback]
""",
        encoding="utf-8",
    )
    lock = Path(".git/auto-coder.lock")
    previous = lock.read_bytes() if lock.exists() else None
    worker_lock = json.dumps({"pid": os.getpid(), "hostname": "test-worker", "started_at": "2026-01-01T00:00:00"}).encode()
    lock.write_bytes(worker_lock)
    environment = os.environ.copy()
    environment.pop("GITHUB_TOKEN", None)
    environment["AUTO_CODER_DISABLE_AUTO_UPDATE"] = "false"
    process = subprocess.Popen(
        [sys.executable, "-m", "auto_coder.cli", "management", "serve", "--config", str(config)],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        response = None
        while process.poll() is None and time.monotonic() < deadline:
            try:
                response = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp",
                    timeout=0.25,
                )
                break
            except OSError:
                time.sleep(0.05)
        assert response is not None, process.communicate(timeout=1)
        assert response.status == 200
        assert json.load(response)["resource"] == f"http://127.0.0.1:{port}/mcp"
        assert lock.read_bytes() == worker_lock
    finally:
        process.terminate()
        process.wait(timeout=10)
        if previous is None:
            lock.unlink(missing_ok=True)
        else:
            lock.write_bytes(previous)
