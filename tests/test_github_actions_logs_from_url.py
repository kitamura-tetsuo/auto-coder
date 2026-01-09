import io
import json
import zipfile
from unittest.mock import Mock, patch

from src.auto_coder.automation_engine import AutomationEngine


def test_get_github_actions_logs_from_url_fetches_job_zip_and_extracts_errors(
    mock_github_client, mock_gemini_client
):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/16949853465/job/48039894437"

    def fake_run(cmd, capture_output=True, text=False, timeout=60, cwd=None):
        # ジョブ名取得
        if cmd[:3] == ["gh", "run", "view"] and "--json" in cmd:
            jobs_obj = {
                "jobs": [
                    {"databaseId": 48039894437, "name": "test", "conclusion": "failure"}
                ]
            }
            return Mock(returncode=0, stdout=json.dumps(jobs_obj), stderr="")
        # job ZIP -> 成功（Playwright失敗を含む1ファイル）
        if cmd[:2] == ["gh", "api"] and "actions/jobs" in cmd[2]:
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w") as zf:
                zf.writestr(
                    "5_Run tests.txt",
                    (
                        "Error:   1) [core] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link \n\n"
                        "    Error: expect(received).toContain(expected) // indexOf\n\n"
                        '    Expected substring: "<a href=\\"https://example.com\\""\n'
                        '    Received string:    "test-page-1755122947471Visit https:/example.comSecond item<!---->"\n\n'
                        "  1 failed\n"
                        "  147 passed\n"
                        "  1 skipped\n"
                        "  151 did not run\n"
                    ),
                )
            return Mock(returncode=0, stdout=bio.getvalue(), stderr=b"")
        return Mock(returncode=1, stdout=b"" if not text else "", stderr=b"unknown")

    with patch("subprocess.run", side_effect=fake_run):
        out = engine.get_github_actions_logs_from_url(url)

    assert "=== Job test (48039894437) ===" in out
    assert "--- Step 5_Run tests ---" in out
    assert 'Expected substring: "<a href="https://example.com"' in out
    # サマリは本文に含まれない行のみ付与されるため、ZIP内の要約行が本文に含まれている場合は省略され得る
