from unittest.mock import patch

from src.auto_coder.automation_engine import AutomationEngine


def test_get_github_actions_logs_from_url_delegates_to_existing_routine(mock_github_client, mock_gemini_client):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/16949853465/job/48039894437"

    with patch.object(AutomationEngine, "_get_github_actions_logs", return_value="delegated logs") as mock_get:
        out = engine.get_github_actions_logs_from_url(url)

    # 既存のルーチンに委譲され、owner/repo と空の failed_checks が渡されること
    mock_get.assert_called_once_with("kitamura-tetsuo/outliner", [])
    assert out == "delegated logs"
