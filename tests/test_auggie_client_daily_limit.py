import io
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.auggie_client import AuggieClient
from src.auto_coder.exceptions import AutoCoderUsageLimitError


def _patch_subprocess(monkeypatch, popen_class):
    """Patch subprocess interactions used by AuggieClient for tests."""

    monkeypatch.setattr(
        "src.auto_coder.auggie_client.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("src.auto_coder.auggie_client.subprocess.Popen", popen_class)


class RecordingPopen:
    """Stub Popen implementation that records invocations."""

    calls: list[list[str]] = []

    def __init__(
        self,
        cmd,
        stdout=None,
        stderr=None,
        text=False,
        bufsize=1,
        universal_newlines=True,
    ):
        type(self).calls.append(list(cmd))
        self.stdout = io.StringIO("stub-response line 1\nstub-response line 2\n")

    def wait(self, timeout=None) -> int:
        return 0


def test_augmie_usage_counter_persists(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_AUGGIE_USAGE_DIR", str(tmp_path))
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    client = AuggieClient()
    output1 = client._run_auggie_cli("first prompt")
    output2 = client._run_auggie_cli("second prompt")

    assert output1.endswith("line 2")
    assert output2.endswith("line 2")
    assert len(RecordingPopen.calls) == 2

    state_path = tmp_path / "auggie_usage.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["count"] == 2
    assert state["date"] == datetime.now().date().isoformat()


def test_auggie_usage_limit_blocks_21st_call(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_AUGGIE_USAGE_DIR", str(tmp_path))
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    state_path = tmp_path / "auggie_usage.json"
    state_path.write_text(
        json.dumps(
            {
                "date": datetime.now().date().isoformat(),
                "count": AuggieClient.DAILY_CALL_LIMIT,
            }
        )
    )

    client = AuggieClient()

    with pytest.raises(AutoCoderUsageLimitError):
        client._run_auggie_cli("blocked prompt")

    assert RecordingPopen.calls == []
    state = json.loads(state_path.read_text())
    assert state["count"] == AuggieClient.DAILY_CALL_LIMIT


def test_auggie_usage_resets_when_date_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_AUGGIE_USAGE_DIR", str(tmp_path))
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
    state_path = tmp_path / "auggie_usage.json"
    state_path.write_text(json.dumps({"date": yesterday, "count": AuggieClient.DAILY_CALL_LIMIT}))

    client = AuggieClient()
    output = client._run_auggie_cli("allowed prompt")

    assert "stub-response" in output
    assert len(RecordingPopen.calls) == 1

    state = json.loads(state_path.read_text())
    assert state["count"] == 1
    assert state["date"] == datetime.now().date().isoformat()


@patch("src.auto_coder.auggie_client.get_llm_config")
def test_auggie_client_options_from_config(mock_get_config, monkeypatch):
    """Test that options are loaded from config and used in CLI commands."""
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    # Mock config to provide options
    mock_config = Mock()
    mock_backend_config = Mock()
    mock_backend_config.model = "GPT-5"
    mock_backend_config.options = ["--print"]
    mock_backend_config.options_for_noedit = ["--print"]
    mock_backend_config.usage_markers = []
    mock_backend_config.validate_required_options.return_value = []
    mock_config.get_backend_config.return_value = mock_backend_config
    mock_get_config.return_value = mock_config

    client = AuggieClient(backend_name="auggie")
    output = client._run_auggie_cli("test prompt")

    # Check that the command was called with options from config
    assert len(RecordingPopen.calls) == 1
    cmd = RecordingPopen.calls[0]
    assert cmd[0] == "auggie"
    assert cmd[1] == "--model"
    assert cmd[2] == "GPT-5"
    assert "--print" in cmd
    assert "test prompt" in cmd


@patch("src.auto_coder.auggie_client.get_llm_config")
def test_auggie_client_multiple_options_from_config(mock_get_config, monkeypatch):
    """Test that multiple options are loaded from config and used in CLI commands."""
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    # Mock config to provide multiple options
    mock_config = Mock()
    mock_backend_config = Mock()
    mock_backend_config.model = "GPT-5"
    mock_backend_config.options = ["--print", "--debug", "--verbose"]
    mock_backend_config.options_for_noedit = ["--print"]
    mock_backend_config.usage_markers = []
    mock_backend_config.validate_required_options.return_value = []
    mock_config.get_backend_config.return_value = mock_backend_config
    mock_get_config.return_value = mock_config

    client = AuggieClient(backend_name="auggie")
    output = client._run_auggie_cli("test prompt")

    # Check that the command was called with all options from config
    assert len(RecordingPopen.calls) == 1
    cmd = RecordingPopen.calls[0]
    assert cmd[0] == "auggie"
    assert cmd[1] == "--model"
    assert cmd[2] == "GPT-5"
    assert "--print" in cmd
    assert "--debug" in cmd
    assert "--verbose" in cmd
    assert "test prompt" in cmd


@patch("src.auto_coder.auggie_client.get_llm_config")
def test_auggie_client_empty_options_default(mock_get_config, monkeypatch):
    """Test that empty options list works (backward compatibility)."""
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    # Mock config with empty options
    mock_config = Mock()
    mock_backend_config = Mock()
    mock_backend_config.model = "GPT-5"
    mock_backend_config.options = []
    mock_backend_config.options_for_noedit = []
    mock_backend_config.usage_markers = []
    mock_backend_config.validate_required_options.return_value = []
    mock_config.get_backend_config.return_value = mock_backend_config
    mock_get_config.return_value = mock_config

    client = AuggieClient(backend_name="auggie")
    output = client._run_auggie_cli("test prompt")

    # Check that the command was called without extra options
    assert len(RecordingPopen.calls) == 1
    cmd = RecordingPopen.calls[0]
    assert cmd[0] == "auggie"
    assert cmd[1] == "--model"
    assert cmd[2] == "GPT-5"
    # Should not have --print or other extra options
    assert "--print" not in cmd
    assert "--debug" not in cmd
    assert "test prompt" in cmd


@patch("src.auto_coder.auggie_client.get_llm_config")
def test_auggie_client_no_backend_config_default(mock_get_config, monkeypatch):
    """Test that None backend config results in empty options (backward compatibility)."""
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    # Mock config to return None for backend
    mock_config = Mock()
    mock_config.get_backend_config.return_value = None
    mock_get_config.return_value = mock_config

    client = AuggieClient(backend_name="auggie")
    output = client._run_auggie_cli("test prompt")

    # Check that the command was called without extra options
    assert len(RecordingPopen.calls) == 1
    cmd = RecordingPopen.calls[0]
    assert cmd[0] == "auggie"
    assert cmd[1] == "--model"
    assert "GPT-5" in cmd  # Default model
    # Should not have --print or other extra options
    assert "--print" not in cmd
    assert "test prompt" in cmd


@patch("src.auto_coder.auggie_client.get_llm_config")
def test_auggie_client_options_for_noedit_stored(mock_get_config, monkeypatch):
    """Test that options_for_noedit is stored from config."""
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    # Mock config to provide options_for_noedit
    mock_config = Mock()
    mock_backend_config = Mock()
    mock_backend_config.model = "GPT-5"
    mock_backend_config.options = ["--print"]
    mock_backend_config.options_for_noedit = ["--print", "--no-edit"]
    mock_backend_config.usage_markers = []
    mock_backend_config.validate_required_options.return_value = []
    mock_config.get_backend_config.return_value = mock_backend_config
    mock_get_config.return_value = mock_config

    client = AuggieClient(backend_name="auggie")

    # Check that options_for_noedit is stored
    assert client.options_for_noedit == ["--print", "--no-edit"]
    assert client.options == ["--print"]
