import unittest
from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.util.github_action import _check_github_actions_status


class TestGitHubActionDeduplication(unittest.TestCase):
    @patch("auto_coder.util.github_action.GitHubClient")
    @patch("auto_coder.util.github_action.get_ghapi_client")
    @patch("auto_coder.util.github_action.get_github_cache")
    def test_same_name_checks_are_not_collapsed_without_execution_association(self, mock_get_cache, mock_get_ghapi_client, mock_github_client):
        # Setup mock cache
        mock_cache_instance = MagicMock()
        mock_cache_instance.get.return_value = None
        mock_get_cache.return_value = mock_cache_instance

        # Setup mock GitHubClient token
        mock_github_client.get_instance.return_value.token = "dummy_token"

        # Mock API response with duplicate check runs
        # GhApi returns Python dicts/lists, not JSON strings
        check_runs = [
            {"name": "Run Tests", "status": "completed", "conclusion": "failure", "head_sha": "sha123", "app": {"id": 1}, "id": 1},
            {"name": "Run Tests", "status": "completed", "conclusion": "success", "head_sha": "sha123", "app": {"id": 1}, "id": 2},
        ]

        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api
        # Mocks api.checks.list_for_ref(owner, repo, ref) -> {"check_runs": ...}
        mock_api.checks.list_for_ref.return_value = {"check_runs": check_runs}
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": []}

        # Call the function
        repo_name = "owner/repo"
        pr_data = {"number": 123, "head": {"sha": "sha123"}}
        config = AutomationConfig()

        result = _check_github_actions_status(repo_name, pr_data, config)

        self.assertFalse(result.success, "Unassociated checks cannot be ordered by timestamps or display name")
        self.assertEqual(result.ids, [], "Check IDs must not be presented as workflow run IDs")


if __name__ == "__main__":
    unittest.main()
