from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.fix_to_pass_tests_runner import apply_workspace_test_fix


class DummyClient:
    def __init__(self, name: str, model_name: str, calls: list[str]):
        self.name = name
        self.model_name = model_name
        self.calls = calls

    def _run_llm_cli(self, prompt: str) -> str:
        self.calls.append(self.name)
        return f"{self.name}:ok"

    def switch_to_default_model(self) -> None:
        pass


def _make_manager(calls: list[str]) -> BackendManager:
    a_client = DummyClient("codex", "m1", calls)

    def fac_codex():
        return DummyClient("codex", "m1", calls)

    def fac_gemini():
        return DummyClient("gemini", "m2", calls)

    return BackendManager(
        default_backend="codex",
        default_client=a_client,
        factories={"codex": fac_codex, "gemini": fac_gemini},
        order=["codex", "gemini"],
    )


@patch(
    "src.auto_coder.fix_to_pass_tests_runner.extract_important_errors",
    return_value="ERR_SUMMARY",
)
def test_apply_workspace_test_fix_switch_after_three_same_test_files(mock_extract):
    cfg = AutomationConfig()
    calls: list[str] = []
    mgr = _make_manager(calls)

    test_result = {
        "success": False,
        "output": "",
        "errors": "boom",
        "command": "bash test.sh",
    }

    # Call three times with same current_test_file
    apply_workspace_test_fix(cfg, test_result, mgr, dry_run=False, current_test_file="test_a.py")
    apply_workspace_test_fix(cfg, test_result, mgr, dry_run=False, current_test_file="test_a.py")
    apply_workspace_test_fix(cfg, test_result, mgr, dry_run=False, current_test_file="test_a.py")

    # First two on codex, third switched to gemini
    assert calls == ["codex", "codex", "gemini"]
