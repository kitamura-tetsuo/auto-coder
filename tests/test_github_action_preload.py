import json
import unittest
from unittest.mock import MagicMock, patch

from src.auto_coder.util.github_action import preload_github_actions_status
from src.auto_coder.util.github_cache import get_github_cache


class TestGithubActionPreload(unittest.TestCase):
    def setUp(self):
        get_github_cache().clear()

    @patch("src.auto_coder.util.github_action.GitHubClient")
    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_preload_github_actions_status(self, mock_get_ghapi_client, mock_github_client):
        # Setup mock API
        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        # Mock list_workflow_runs_for_repo response
        # Using snake_case keys as typical for GhApi/GitHub REST API
        runs_data = [
            {"id": 12345, "head_sha": "sha12345", "status": "completed", "conclusion": "success"},
            {"id": 67890, "head_sha": "sha67890", "status": "completed", "conclusion": "failure"},
        ]
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": runs_data}

        # Input PRs
        prs = [{"number": 1, "head": {"sha": "sha12345"}}, {"number": 2, "head": {"sha": "sha67890"}}, {"number": 3, "head": {"sha": "sha_missing"}}]

        # Execute
        preload_github_actions_status("owner/repo", prs)

        # Verify API called once
        mock_api.actions.list_workflow_runs_for_repo.assert_called_once_with("owner", "repo", per_page=100)

        # Verify cache population
        cache = get_github_cache()

        # PR 1 should be success
        key1 = "gh_actions_status:owner/repo:1:sha12345"
        res1 = cache.get(key1)
        self.assertIsNotNone(res1)
        self.assertTrue(res1.success)
        self.assertIn(12345, res1.ids)

        # PR 2 should be failure
        key2 = "gh_actions_status:owner/repo:2:sha67890"
        res2 = cache.get(key2)
        self.assertIsNotNone(res2)
        self.assertFalse(res2.success)
        self.assertIn(67890, res2.ids)

        # PR 3 should NOT be in cache (we don't cache missing runs)
        key3 = "gh_actions_status:owner/repo:3:sha_missing"
        res3 = cache.get(key3)
        self.assertIsNone(res3)
