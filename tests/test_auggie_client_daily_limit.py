import io
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from auto_coder.auggie_client import AuggieClient
from auto_coder.exceptions import AutoCoderUsageLimitError


def _patch_subprocess(monkeypatch, popen_class):
    """Patch subprocess interactions used by AuggieClient for tests."""

    monkeypatch.setattr(
        "auto_coder.auggie_client.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("auto_coder.auggie_client.subprocess.Popen", popen_class)


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

    def wait(self) -> int:
        return 0


def test_auggie_usage_counter_persists(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_AUGGIE_USAGE_DIR", str(tmp_path))
    RecordingPopen.calls = []
    _patch_subprocess(monkeypatch, RecordingPopen)

    client = AuggieClient(model_name="GPT-5")
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
