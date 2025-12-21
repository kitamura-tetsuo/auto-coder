import unittest
from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.util.github_action import _check_github_actions_status


class TestGitHubActionDeduplication(unittest.TestCase):
    @patch("auto_coder.util.github_action.GitHubClient")
    @patch("auto_coder.util.github_action.get_ghapi_client")
    @patch("auto_coder.util.github_action.get_github_cache")
    def test_duplicate_check_runs_are_deduplicated(self, mock_get_cache, mock_get_ghapi_client, mock_github_client):
        # Setup mock cache
        mock_cache_instance = MagicMock()
        mock_cache_instance.get.return_value = None
        mock_get_cache.return_value = mock_cache_instance

        # Setup mock GitHubClient token
        mock_github_client.get_instance.return_value.token = "dummy_token"

        # Mock API response with duplicate check runs
        # GhApi returns Python dicts/lists, not JSON strings
        check_runs = [
            {"name": "Run Tests", "status": "completed", "conclusion": "failure", "started_at": "2023-10-27T10:00:00Z", "completed_at": "2023-10-27T10:05:00Z", "html_url": "https://github.com/owner/repo/actions/runs/1", "id": 1},
            {"name": "Run Tests", "status": "completed", "conclusion": "success", "started_at": "2023-10-27T10:10:00Z", "completed_at": "2023-10-27T10:15:00Z", "html_url": "https://github.com/owner/repo/actions/runs/2", "id": 2},
        ]

        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api
        # Mocks api.checks.list_for_ref(owner, repo, ref) -> {"check_runs": ...}
        mock_api.checks.list_for_ref.return_value = {"check_runs": check_runs}

        # Call the function
        repo_name = "owner/repo"
        pr_data = {"number": 123, "head": {"sha": "sha123"}}
        config = AutomationConfig()

        result = _check_github_actions_status(repo_name, pr_data, config)

        # Before fix, this should fail because it sees the failure
        # After fix, this should pass because it only sees the success (latest)
        self.assertTrue(result.success, "Should report success when latest run is successful, ignoring older failed run")
        self.assertEqual(len(result.ids), 1, "Should only report one run ID (the latest)")
        self.assertEqual(result.ids[0], 2, "Should report the latest run ID")


if __name__ == "__main__":
    unittest.main()
