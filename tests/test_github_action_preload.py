import json
import unittest
from unittest.mock import MagicMock, patch

from src.auto_coder.util.github_action import preload_github_actions_status
from src.auto_coder.util.github_cache import get_github_cache


class TestGithubActionPreload(unittest.TestCase):
    def setUp(self):
        get_github_cache().clear()

    @patch("src.auto_coder.util.github_action.get_gh_logger")
    def test_preload_github_actions_status(self, mock_get_gh_logger):
        # Setup mock logger
        mock_logger = MagicMock()
        mock_get_gh_logger.return_value = mock_logger

        # Mock gh run list response
        mock_run_result = MagicMock()
        mock_run_result.returncode = 0
        mock_run_result.success = True

        # Sample runs
        runs_data = [{"databaseId": 12345, "headSha": "sha12345", "status": "completed", "conclusion": "success"}, {"databaseId": 67890, "headSha": "sha67890", "status": "completed", "conclusion": "failure"}]
        mock_run_result.stdout = json.dumps(runs_data)
        mock_logger.execute_with_logging.return_value = mock_run_result

        # Input PRs
        prs = [{"number": 1, "head": {"sha": "sha12345"}}, {"number": 2, "head": {"sha": "sha67890"}}, {"number": 3, "head": {"sha": "sha_missing"}}]

        # Execute
        preload_github_actions_status("owner/repo", prs)

        # Verify execute_with_logging called once
        mock_logger.execute_with_logging.assert_called_once()
        args, _ = mock_logger.execute_with_logging.call_args
        self.assertIn("gh", args[0])
        self.assertIn("run", args[0])
        self.assertIn("list", args[0])

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
