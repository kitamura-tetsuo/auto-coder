import io
import json
import zipfile
from unittest.mock import Mock, patch

from src.auto_coder.automation_engine import AutomationEngine


def test_get_github_actions_logs_uses_gh_api_and_extracts_errors(
    mock_github_client, mock_gemini_client
):
    """gh api でジョブログ(zip)を取得し、エラー行を抜粋できることを検証する。"""
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    repo_name = "kitamura-tetsuo/outliner"
    pr_data = {
        "number": 457,
        "title": "Test PR",
        "head_branch": "codex/add-comprehensive-tests-for-component",
    }

    # side_effect でコマンド毎の戻りを切替
    def fake_run(cmd, capture_output=True, text=False, timeout=60, cwd=None):
        if cmd[:3] == ["gh", "run", "list"]:
            runs = [
                {
                    "databaseId": 16818157306,
                    "headBranch": pr_data["head_branch"],
                    "conclusion": "failure",
                    "createdAt": "2025-08-09T00:00:00Z",
                    "status": "completed",
                    "displayTitle": "CI",
                    "url": "https://example.com/run/16818157306",
                }
            ]
            return Mock(returncode=0, stdout=json.dumps(runs), stderr="")
        if cmd[:3] == ["gh", "run", "view"]:
            jobs_obj = {
                "jobs": [
                    {
                        "databaseId": 47639576037,
                        "name": "build",
                        "conclusion": "failure",
                    }
                ]
            }
            return Mock(returncode=0, stdout=json.dumps(jobs_obj), stderr="")
        if cmd[:2] == ["gh", "api"] and "actions/jobs" in cmd[2]:
            # zip をメモリ上で作って返す
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w") as zf:
                zf.writestr("1_log.txt", "INFO: ok\nERROR: boom!\nmore info")
            return Mock(returncode=0, stdout=bio.getvalue(), stderr=b"")
        # デフォルト
        return Mock(returncode=1, stdout="", stderr="unknown command")

    with patch("subprocess.run", side_effect=fake_run):
        out = engine._get_github_actions_logs(
            repo_name, pr_data, [{"name": "ci", "conclusion": "failure"}]
        )

    assert "=== Job build (47639576037) ===" in out
    assert "ERROR: boom!" in out
