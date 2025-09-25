import importlib
import os
import subprocess
from pathlib import Path

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.logger_config import setup_logger
from src.auto_coder.test_runner import fix_to_pass_tests


class StubLLMClient:
    """Minimal LLM client that records invocations and simulates fixes."""

    def __init__(self, marker_path: Path) -> None:
        self.marker_path = marker_path
        self.backend = "codex"
        self.model_name = "gpt-4o-mini"
        self.invocations: list[str] = []

    def get_last_backend_and_model(self) -> tuple[str, str]:
        return self.backend, self.model_name

    def _run_llm_cli(self, prompt: str) -> str:
        self.marker_path.write_text("applied", encoding="utf-8")
        self.invocations.append(prompt)
        return "Applied stub workspace fix"


def _write_test_script(script_path: Path, marker_dir: Path) -> None:
    state_file = marker_dir / "state.txt"
    llm_marker = marker_dir / "llm_applied.txt"
    script = f"""#!/usr/bin/env bash
set -e
STATE_FILE={state_file!s}
LLM_FILE={llm_marker!s}
TARGET_PATH="src/tests/unit/cursor/cursor-core.spec.ts"

if [ "$#" -gt 0 ]; then
  if [ -f "$LLM_FILE" ]; then
    echo "Targeted rerun after LLM fix succeeded for $1"
    echo "llm_target_passed" > "$STATE_FILE"
    exit 0
  fi
  echo "Targeted rerun still failing for $1"
  echo "target_failed_pre_llm" > "$STATE_FILE"
  exit 1
fi

if [ ! -f "$STATE_FILE" ]; then
  echo "✘ 1 [suite] › $TARGET_PATH:12:5 › cursor flow fails"
  echo "Expected substring: foo"
  echo "Received string: bar"
  echo "FAILED $TARGET_PATH::test_cursor_flow - AssertionError"
  echo "full_failed" > "$STATE_FILE"
  exit 1
fi

STATE=$(cat "$STATE_FILE")
if [ "$STATE" = "target_failed_pre_llm" ]; then
  if [ -f "$LLM_FILE" ]; then
    echo "Full suite succeeded after LLM fix"
    echo "llm_full_passed" > "$STATE_FILE"
    exit 0
  fi
  echo "✘ 1 [suite] › $TARGET_PATH:12:5 › cursor flow still failing"
  echo "Received string: bar"
  exit 1
fi

if [ "$STATE" = "llm_target_passed" ] || [ "$STATE" = "llm_full_passed" ]; then
  echo "All tests passed"
  exit 0
fi

echo "✘ Unexpected state during test execution"
exit 1
"""
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)


def test_fix_to_pass_tests_verbose_flow_logging_e2e(tmp_path):
    repo_dir = tmp_path
    scripts_dir = repo_dir / "scripts"
    scripts_dir.mkdir()
    markers_dir = repo_dir / "markers"
    markers_dir.mkdir()

    test_script = scripts_dir / "test.sh"
    _write_test_script(test_script, markers_dir)

    from src.auto_coder import utils as utils_module

    original_utils_subprocess = utils_module.subprocess
    real_subprocess = importlib.reload(subprocess)
    utils_module.subprocess = real_subprocess

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "auto@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Auto Coder"], cwd=repo_dir, check=True)

    (repo_dir / "README.md").write_text("sample", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", "scripts/test.sh"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    log_file = repo_dir / "runner.log"
    setup_logger(log_level="DEBUG", log_file=str(log_file))

    original_update_flag = os.environ.get("AUTO_CODER_DISABLE_AUTO_UPDATE")
    os.environ["AUTO_CODER_DISABLE_AUTO_UPDATE"] = "1"

    original_cwd = os.getcwd()
    os.chdir(repo_dir)
    try:
        config = AutomationConfig()
        llm_marker = markers_dir / "llm_applied.txt"
        llm_client = StubLLMClient(llm_marker)

        result = fix_to_pass_tests(config, dry_run=False, max_attempts=5, llm_client=llm_client)

        assert result["success"] is True
        assert result["attempts"] >= 3
        assert llm_marker.exists()
        assert llm_client.invocations, "LLM should be invoked at least once"

        log_text = log_file.read_text(encoding="utf-8")
        assert "Detected failing test file src/tests/unit/cursor/cursor-core.spec.ts" in log_text
        assert (
            "Targeted test src/tests/unit/cursor/cursor-core.spec.ts passed after LLM fix"
            in log_text
        )
        assert "Requesting LLM workspace fix using backend codex model gpt-4o-mini" in log_text
        assert "LLM workspace fix summary: Applied stub workspace fix" in log_text
    finally:
        os.chdir(original_cwd)
        if original_update_flag is None:
            os.environ.pop("AUTO_CODER_DISABLE_AUTO_UPDATE", None)
        else:
            os.environ["AUTO_CODER_DISABLE_AUTO_UPDATE"] = original_update_flag
        utils_module.subprocess = original_utils_subprocess

