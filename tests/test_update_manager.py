import json
import subprocess
from unittest.mock import patch

import pytest

from src.auto_coder import update_manager


@pytest.fixture(autouse=True)
def clear_disable_flag(monkeypatch):
    monkeypatch.delenv("AUTO_CODER_DISABLE_AUTO_UPDATE", raising=False)
    monkeypatch.delenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("AUTO_CODER_UPDATE_STATE_DIR", raising=False)


def test_maybe_run_auto_update_skips_outside_pipx(monkeypatch):
    with patch("src.auto_coder.update_manager._running_inside_pipx_env", return_value=False):
        with patch("src.auto_coder.update_manager.shutil.which") as mock_which:
            update_manager.maybe_run_auto_update()
            mock_which.assert_not_called()


def test_maybe_run_auto_update_runs_pipx(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")

    fake_result = subprocess.CompletedProcess(
        args=["pipx", "upgrade", "auto-coder"], returncode=0, stdout="updated", stderr=""
    )

    with patch("src.auto_coder.update_manager._running_inside_pipx_env", return_value=True), \
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/pipx"), \
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result) as mock_run:
        update_manager.maybe_run_auto_update()
        mock_run.assert_called_once_with(
            ["/usr/bin/pipx", "upgrade", "auto-coder"],
            capture_output=True,
            text=True,
            timeout=900,
        )

    state_path = state_dir / "update_state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["last_result"] == "success"
    assert state["last_error"] == ""


def test_maybe_run_auto_update_reports_failure(monkeypatch, tmp_path, capsys):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")

    fake_result = subprocess.CompletedProcess(
        args=["pipx", "upgrade", "auto-coder"], returncode=1, stdout="", stderr="network error"
    )

    with patch("src.auto_coder.update_manager._running_inside_pipx_env", return_value=True), \
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/pipx"), \
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result):
        update_manager.maybe_run_auto_update()

    err = capsys.readouterr().err
    assert "Auto-Coder auto-update could not be completed" in err
    assert "pipx upgrade auto-coder" in err

    state_path = state_dir / "update_state.json"
    state = json.loads(state_path.read_text())
    assert state["last_result"] == "failure"
    assert "network error" in state["last_error"]


def test_maybe_run_auto_update_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("AUTO_CODER_DISABLE_AUTO_UPDATE", "1")
    with patch("src.auto_coder.update_manager._running_inside_pipx_env") as mock_env:
        update_manager.maybe_run_auto_update()
    mock_env.assert_not_called()
