import io
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from click.testing import CliRunner

from src.auto_coder.cli import process_issues
from src.auto_coder.auggie_client import AuggieClient


class DummyPopen:
    """Stub Popen used to detect unwanted Auggie invocations."""

    calls = 0

    def __init__(self, cmd, stdout=None, stderr=None, text=False, bufsize=1, universal_newlines=True):
        type(self).calls += 1
        self.stdout = io.StringIO("should-not-run\n")

    def wait(self) -> int:
        return 0


class RecordingGeminiClient:
    """GeminiClient stub capturing prompts passed via BackendManager."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        self.model_name = kwargs.get("model_name", "gemini-2.5-pro")

    def _run_llm_cli(self, prompt: str) -> str:
        type(self).calls.append(prompt)
        return "gemini-fallback"


class FakeAutomationEngine:
    """AutomationEngine stub that exercises the BackendManager."""

    instances: list["FakeAutomationEngine"] = []
    last_output: str = ""

    def __init__(self, github_client, llm_client, dry_run=False, config=None):
        self.github = github_client
        self.llm = llm_client
        type(self).instances.append(self)

    def run(self, repo_name, jules_mode=False):  # pragma: no cover - exercised via CLI
        type(self).last_output = self.llm._run_llm_cli("Daily limit fallback verification prompt")
        return {"llm_output": type(self).last_output}


def test_process_issues_rotates_when_auggie_daily_limit_reached(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_AUGGIE_USAGE_DIR", str(tmp_path))
    RecordingGeminiClient.calls = []
    DummyPopen.calls = 0
    FakeAutomationEngine.instances = []
    FakeAutomationEngine.last_output = ""

    state_path = tmp_path / "auggie_usage.json"
    state_path.write_text(
        json.dumps(
            {
                "date": datetime.now().date().isoformat(),
                "count": AuggieClient.DAILY_CALL_LIMIT,
            }
        )
    )

    monkeypatch.setattr(
        "src.auto_coder.auggie_client.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("src.auto_coder.auggie_client.subprocess.Popen", DummyPopen)

    monkeypatch.setattr("src.auto_coder.cli.check_auggie_cli_or_fail", lambda: None)
    monkeypatch.setattr("src.auto_coder.cli.check_gemini_cli_or_fail", lambda: None)
    monkeypatch.setattr("src.auto_coder.cli.GitHubClient", lambda token: Mock())
    monkeypatch.setattr("src.auto_coder.cli.GeminiClient", RecordingGeminiClient)
    monkeypatch.setattr("src.auto_coder.cli.AutomationEngine", FakeAutomationEngine)

    runner = CliRunner()
    result = runner.invoke(
        process_issues,
        [
            "--repo",
            "owner/repo",
            "--github-token",
            "token",
            "--backend",
            "auggie",
            "--backend",
            "gemini",
        ],
    )

    assert result.exit_code == 0, result.output
    assert DummyPopen.calls == 0
    assert RecordingGeminiClient.calls == ["Daily limit fallback verification prompt"]
    assert FakeAutomationEngine.instances, "Automation engine should have been instantiated"
    assert FakeAutomationEngine.last_output == "gemini-fallback"

    state = json.loads(state_path.read_text())
    assert state["count"] == AuggieClient.DAILY_CALL_LIMIT
    assert state["date"] == datetime.now().date().isoformat()

