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


# ---- _detect_install_method tests ----


def test_detect_install_method_returns_pipx_from_prefix(monkeypatch):
    """_detect_install_method should return 'pipx' when sys.prefix contains 'pipx'."""
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    with patch("src.auto_coder.update_manager.sys") as mock_sys:
        mock_sys.prefix = "/home/user/.local/pipx/venvs/auto-coder"
        result = update_manager._detect_install_method()
    assert result == "pipx"


def test_detect_install_method_returns_pipx_from_env(monkeypatch):
    """_detect_install_method should return 'pipx' when PIPX_HOME is set."""
    monkeypatch.setenv("PIPX_HOME", "/home/user/.local/pipx")
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    with patch("src.auto_coder.update_manager.sys") as mock_sys:
        mock_sys.prefix = "/usr"
        result = update_manager._detect_install_method()
    assert result == "pipx"


def test_detect_install_method_returns_uv_from_prefix(monkeypatch):
    """_detect_install_method should return 'uv' when sys.prefix contains 'uv'."""
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    with patch("src.auto_coder.update_manager.sys") as mock_sys:
        mock_sys.prefix = "/home/user/.local/share/uv/tools/auto-coder"
        result = update_manager._detect_install_method()
    assert result == "uv"


def test_detect_install_method_returns_uv_from_env(monkeypatch):
    """_detect_install_method should return 'uv' when UV_TOOL_DIR is set."""
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.setenv("UV_TOOL_DIR", "/home/user/.local/share/uv/tools")
    with patch("src.auto_coder.update_manager.sys") as mock_sys:
        mock_sys.prefix = "/usr"
        result = update_manager._detect_install_method()
    assert result == "uv"


def test_detect_install_method_returns_none(monkeypatch):
    """_detect_install_method should return None when neither pipx nor uv is detected."""
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    with patch("src.auto_coder.update_manager.sys") as mock_sys:
        mock_sys.prefix = "/usr"
        result = update_manager._detect_install_method()
    assert result is None


def test_detect_install_method_pipx_takes_precedence(monkeypatch):
    """When both pipx and uv indicators are present, pipx should win."""
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    with patch("src.auto_coder.update_manager.sys") as mock_sys:
        # Path contains both 'pipx' and 'uv' — pipx is checked first
        mock_sys.prefix = "/home/user/.local/pipx/venvs/uv/auto-coder"
        result = update_manager._detect_install_method()
    assert result == "pipx"


# ---- maybe_run_auto_update tests (pipx path) ----


def test_maybe_run_auto_update_skips_outside_managed_env(monkeypatch):
    with patch("src.auto_coder.update_manager._detect_install_method", return_value=None):
        with patch("src.auto_coder.update_manager.shutil.which") as mock_which:
            result = update_manager.maybe_run_auto_update()
            mock_which.assert_not_called()
            assert result.attempted is False
            assert result.updated is False
            assert result.reason == "outside-managed-env"


def test_maybe_run_auto_update_runs_pipx(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")

    fake_result = subprocess.CompletedProcess(
        args=["pipx", "upgrade", "auto-coder"],
        returncode=0,
        stdout="updated",
        stderr="",
    )

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="pipx"),
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/pipx"),
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result) as mock_run,
    ):
        result = update_manager.maybe_run_auto_update()
        mock_run.assert_called_once_with(
            ["/usr/bin/pipx", "upgrade", "auto-coder"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert result.attempted is True
        assert result.updated is True
        assert result.stdout == "updated"

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
        args=["pipx", "upgrade", "auto-coder"],
        returncode=1,
        stdout="",
        stderr="network error",
    )

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="pipx"),
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/pipx"),
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result),
    ):
        result = update_manager.maybe_run_auto_update()

    err = capsys.readouterr().err
    assert "Auto-Coder auto-update could not be completed" in err
    assert "pipx upgrade auto-coder" in err
    assert result.attempted is True
    assert result.updated is False
    assert "network error" in result.reason

    state_path = state_dir / "update_state.json"
    state = json.loads(state_path.read_text())
    assert state["last_result"] == "failure"
    assert "network error" in state["last_error"]


def test_maybe_run_auto_update_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("AUTO_CODER_DISABLE_AUTO_UPDATE", "1")
    with patch("src.auto_coder.update_manager._detect_install_method") as mock_detect:
        result = update_manager.maybe_run_auto_update()
    mock_detect.assert_not_called()
    assert result.attempted is False
    assert result.updated is False
    assert result.reason == "disabled"


# ---- maybe_run_auto_update tests (uv path) ----


def test_maybe_run_auto_update_runs_uv(monkeypatch, tmp_path):
    """When inside a uv tool environment, should run 'uv tool upgrade auto-coder'."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")

    fake_result = subprocess.CompletedProcess(
        args=["uv", "tool", "upgrade", "auto-coder"],
        returncode=0,
        stdout="Updated auto-coder v2026.5.15 -> v2026.5.16",
        stderr="",
    )

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="uv"),
        patch("src.auto_coder.update_manager.shutil.which", return_value="/home/node/.local/bin/uv"),
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result) as mock_run,
    ):
        result = update_manager.maybe_run_auto_update()
        mock_run.assert_called_once_with(
            ["/home/node/.local/bin/uv", "tool", "upgrade", "auto-coder"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert result.attempted is True
        assert result.updated is True
        assert "Updated" in result.stdout

    state_path = state_dir / "update_state.json"
    state = json.loads(state_path.read_text())
    assert state["last_result"] == "success"


def test_maybe_run_auto_update_uv_no_changes(monkeypatch, tmp_path):
    """When uv tool upgrade reports no changes, updated should be False."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")

    fake_result = subprocess.CompletedProcess(
        args=["uv", "tool", "upgrade", "auto-coder"],
        returncode=0,
        stdout="Nothing to do\nauto-coder is already up to date",
        stderr="",
    )

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="uv"),
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/uv"),
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result),
    ):
        result = update_manager.maybe_run_auto_update()
        assert result.attempted is True
        assert result.updated is False


def test_maybe_run_auto_update_uv_missing(monkeypatch, tmp_path, capsys):
    """When uv executable is missing, should notify and return uv-missing reason."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="uv"),
        patch("src.auto_coder.update_manager.shutil.which", return_value=None),
    ):
        result = update_manager.maybe_run_auto_update()

    err = capsys.readouterr().err
    assert "uv tool upgrade auto-coder" in err
    assert result.attempted is False
    assert result.reason == "uv-missing"


def test_maybe_run_auto_update_uv_failure(monkeypatch, tmp_path, capsys):
    """When uv tool upgrade fails, should notify with uv-specific message."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")

    fake_result = subprocess.CompletedProcess(
        args=["uv", "tool", "upgrade", "auto-coder"],
        returncode=1,
        stdout="",
        stderr="error: No tool installed for package `auto-coder`",
    )

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="uv"),
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/uv"),
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result),
    ):
        result = update_manager.maybe_run_auto_update()

    err = capsys.readouterr().err
    assert "uv tool upgrade auto-coder" in err
    assert result.attempted is True
    assert result.updated is False


# ---- _uv_upgrade_indicated_change tests ----


def test_uv_upgrade_indicated_change_positive():
    assert update_manager._uv_upgrade_indicated_change("Updated auto-coder v1 -> v2", "") is True
    assert update_manager._uv_upgrade_indicated_change("Resolved 3 packages", "") is True
    assert update_manager._uv_upgrade_indicated_change("Prepared 1 package", "") is True


def test_uv_upgrade_indicated_change_negative():
    assert update_manager._uv_upgrade_indicated_change("Nothing to do", "") is False
    assert update_manager._uv_upgrade_indicated_change("", "already up to date") is False
    assert update_manager._uv_upgrade_indicated_change("unchanged", "") is False


# ---- check_for_updates_and_restart tests ----


def test_check_for_updates_and_restart_triggers_capture(monkeypatch, tmp_path):
    marker = tmp_path / "restart.json"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("AUTO_CODER_TEST_CAPTURE_RESTART", str(marker))

    update_manager.record_startup_options(["auto-coder", "fix-to-pass-tests"], {"PATH": "/tmp"})

    fake_result = subprocess.CompletedProcess(
        args=["pipx", "upgrade", "auto-coder"],
        returncode=0,
        stdout="upgraded package auto-coder from 0.0.1 to 0.0.2",
        stderr="",
    )

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="pipx"),
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/pipx"),
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result),
    ):
        with pytest.raises(SystemExit) as exc:
            update_manager.check_for_updates_and_restart()

    assert exc.value.code == 0
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["argv"] == ["auto-coder", "fix-to-pass-tests"]
    assert payload["env"]["PATH"] == "/tmp"


def test_check_for_updates_and_restart_with_uv(monkeypatch, tmp_path):
    """check_for_updates_and_restart should work with uv tool installations."""
    marker = tmp_path / "restart.json"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTO_CODER_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AUTO_CODER_UPDATE_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("AUTO_CODER_TEST_CAPTURE_RESTART", str(marker))

    update_manager.record_startup_options(["auto-coder", "process-issues"], {"PATH": "/tmp"})

    fake_result = subprocess.CompletedProcess(
        args=["uv", "tool", "upgrade", "auto-coder"],
        returncode=0,
        stdout="Updated auto-coder v2026.5.15 -> v2026.5.16",
        stderr="",
    )

    with (
        patch("src.auto_coder.update_manager._detect_install_method", return_value="uv"),
        patch("src.auto_coder.update_manager.shutil.which", return_value="/usr/bin/uv"),
        patch("src.auto_coder.update_manager.subprocess.run", return_value=fake_result),
    ):
        with pytest.raises(SystemExit) as exc:
            update_manager.check_for_updates_and_restart()

    assert exc.value.code == 0
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["argv"] == ["auto-coder", "process-issues"]


def test_restart_without_recorded_command():
    # Clear previously recorded state
    update_manager.record_startup_options([])
    with patch("src.auto_coder.update_manager.os.execvpe") as mock_exec:
        update_manager.restart_with_startup_options()
    mock_exec.assert_not_called()
