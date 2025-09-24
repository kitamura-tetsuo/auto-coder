import json
import os
import subprocess
import sys
from pathlib import Path

from .test_cli_auto_update_e2e import _build_env
from .test_fix_to_pass_tests_restart_e2e import _write_fake_upgrading_pipx


def _write_restart_runner(script_path: Path, capture_path: Path) -> None:
    script_path.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "\n"
        "from src.auto_coder.automation_config import AutomationConfig\n"
        "from src.auto_coder.pr_processor import process_pull_requests\n"
        "from src.auto_coder.update_manager import record_startup_options\n"
        "\n"
        "class DummyGitHubClient:\n"
        "    def __init__(self, marker: Path):\n"
        "        self.marker = marker\n"
        "\n"
        "    def _record(self, event):\n"
        "        data = []\n"
        "        if self.marker.exists():\n"
        "            try:\n"
        "                data = json.loads(self.marker.read_text())\n"
        "            except Exception:\n"
        "                data = []\n"
        "        data.append(event)\n"
        "        self.marker.write_text(json.dumps(data))\n"
        "\n"
        "    def get_open_pull_requests(self, repo_name, limit=None):\n"
        "        self._record({'event': 'get_open_pull_requests', 'repo': repo_name})\n"
        "        return [SimpleNamespace(number=1), SimpleNamespace(number=2)]\n"
        "\n"
        "    def get_pr_details(self, pr):\n"
        "        self._record({'event': 'get_pr_details', 'number': pr.number})\n"
        "        return {'number': pr.number, 'title': f'PR {pr.number}', 'mergeable': True}\n"
        "\n"
        "record_startup_options(sys.argv, os.environ)\n"
        "cfg = AutomationConfig()\n"
        "cfg.max_prs_per_run = -1\n"
        "marker = Path(os.environ['AUTO_CODER_TEST_GITHUB_MARKER'])\n"
        "client = DummyGitHubClient(marker)\n"
        "\n"
        "# Ensure deterministic behavior for PR processing\n"
        "from src.auto_coder import pr_processor as pr_module\n"
        "pr_module._check_github_actions_status = lambda repo, pr_data, config: {'success': False}\n"
        "pr_module._process_pr_for_fixes = lambda repo, pr_data, config, dry_run: {'pr_data': pr_data, 'actions_taken': [], 'priority': 'fix'}\n"
        "\n"
        "process_pull_requests(client, cfg, dry_run=True, repo_name='owner/repo')\n"
        "raise RuntimeError('Expected restart before completing PR processing')\n",
        encoding="utf-8",
    )


def test_process_issues_pr_loop_triggers_restart_on_update(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_upgrading_pipx(bin_dir / "pipx")

    codex_stub = bin_dir / "codex"
    codex_stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'codex stub'\n"
        "  exit 0\n"
        "fi\n"
        "echo 'codex stub invoked' >&2\n"
        "exit 0\n",
        encoding="utf-8",
    )
    codex_stub.chmod(0o755)

    env = _build_env(tmp_path)
    env["AUTOCODER_CODEX_CLI"] = "echo codex"

    restart_marker = tmp_path / "restart.json"
    env["AUTO_CODER_TEST_CAPTURE_RESTART"] = str(restart_marker)

    repo_root = Path(__file__).resolve().parents[1]
    github_marker = tmp_path / "github-events.json"
    github_marker.write_text("[]", encoding="utf-8")
    env["AUTO_CODER_TEST_GITHUB_MARKER"] = str(github_marker)

    existing_py_path = env.get("PYTHONPATH") or ""
    extra_paths = [str(repo_root), str(repo_root / "src")]
    if existing_py_path:
        extra_paths.append(existing_py_path)
    env["PYTHONPATH"] = os.pathsep.join(extra_paths)

    runner_script = tmp_path / "run_restart.py"
    _write_restart_runner(runner_script, restart_marker)

    completed = subprocess.run(
        [sys.executable, str(runner_script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert restart_marker.exists()
    restart_payload = json.loads(restart_marker.read_text())
    assert "run_restart.py" in Path(restart_payload["argv"][0]).name

    pipx_marker = bin_dir / "pipx_invocation.json"
    assert pipx_marker.exists()
    invocations = json.loads(pipx_marker.read_text())
    assert len(invocations) >= 1
    assert invocations[0]["argv"][1:] == ["upgrade", "auto-coder"]

    events = json.loads(github_marker.read_text())
    assert any(evt.get("event") == "get_open_pull_requests" for evt in events)
