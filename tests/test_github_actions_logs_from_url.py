import io
import json
import zipfile
from unittest.mock import Mock, patch

from src.auto_coder.automation_engine import AutomationEngine


def test_get_github_actions_logs_from_url(mock_github_client, mock_gemini_client):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/16949853465/job/48039894437"

    repo_name = "kitamura-tetsuo/outliner"
    job_id = "48039894437"

    def fake_run(cmd, capture_output=True, text=False, timeout=60, cwd=None):
        if cmd[:5] == [
            "gh",
            "api",
            f"repos/{repo_name}/actions/jobs/{job_id}",
            "--json",
            "name",
        ]:
            return Mock(returncode=0, stdout=json.dumps({"name": "build"}), stderr="")
        if cmd[:4] == [
            "gh",
            "api",
            f"repos/{repo_name}/actions/jobs/{job_id}/logs",
        ]:
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w") as zf:
                zf.writestr("log.txt", "INFO ok\nERROR boom\n")
            return Mock(returncode=0, stdout=bio.getvalue(), stderr=b"")
        return Mock(returncode=1, stdout="", stderr="unknown command")

    with patch("subprocess.run", side_effect=fake_run):
        logs = engine.get_github_actions_logs_from_url(url)

    assert "Job build (48039894437)" in logs
    assert "ERROR boom" in logs
