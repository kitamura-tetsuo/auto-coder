
import unittest
from unittest.mock import MagicMock, patch
from auto_coder.util.github_action import trigger_workflow_dispatch

class TestTriggerWorkflowDispatch(unittest.TestCase):
    @patch("auto_coder.util.github_action.get_ghapi_client")
    @patch("auto_coder.util.github_action.GitHubClient")
    def test_trigger_workflow_dispatch_arguments(self, mock_gh_client_cls, mock_get_ghapi):
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        repo_name = "owner/repo"
        workflow_id = "ci.yml"
        ref = "main"

        # Execute
        trigger_workflow_dispatch(repo_name, workflow_id, ref)

        # Verify
        mock_api.actions.create_workflow_dispatch.assert_called_once()
        args, kwargs = mock_api.actions.create_workflow_dispatch.call_args

        # Verify positional args (owner, repo, workflow_id)
        self.assertEqual(args[0], "owner")
        self.assertEqual(args[1], "repo")
        self.assertEqual(args[2], "ci.yml")

        # Verify 'ref' is passed as keyword argument
        self.assertIn('ref', kwargs)
        self.assertEqual(kwargs['ref'], "main")

        # Ensure it was NOT passed positionally (should only be 3 args)
        self.assertEqual(len(args), 3, "Should not have 4th positional argument")


    @patch("auto_coder.util.github_action.get_ghapi_client")
    @patch("auto_coder.util.github_action.GitHubClient")
    def test_trigger_workflow_dispatch_fallback(self, mock_gh_client_cls, mock_get_ghapi):
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        repo_name = "owner/repo"
        workflow_id = "ci.yml"
        ref = "feature-branch"

        # Mock create_workflow_dispatch to raise 422 first, then succeed
        mock_api.actions.create_workflow_dispatch.side_effect = [
            Exception("422 Unprocessable Entity"),  # First call fails
            None  # Second call succeeds
        ]

        # Mock get_content to return file without workflow_dispatch
        import base64
        # Original content: only 'on: push'
        yaml_content = "name: CI\non:\n  push:\n    branches: [main]\n"
        encoded_content = base64.b64encode(yaml_content.encode("utf-8")).decode("utf-8")

        mock_api.repos.get_content.return_value = {
            "content": encoded_content,
            "sha": "sha123"
        }

        # Execute
        with patch("time.sleep"):
             result = trigger_workflow_dispatch(repo_name, workflow_id, ref)

        # Verify
        self.assertTrue(result)

        # Check create_workflow_dispatch called twice
        self.assertEqual(mock_api.actions.create_workflow_dispatch.call_count, 2)

        # Check get_content called
        mock_api.repos.get_content.assert_called_once_with("owner", "repo", ".github/workflows/ci.yml", ref=ref)

        # Check create_or_update_file_contents called
        mock_api.repos.create_or_update_file_contents.assert_called_once()
        call_kwargs = mock_api.repos.create_or_update_file_contents.call_args[1]

        self.assertEqual(call_kwargs['branch'], ref)
        self.assertEqual(call_kwargs['message'], f"Auto-Coder: Add workflow_dispatch trigger to {workflow_id}")
        self.assertEqual(call_kwargs['sha'], "sha123")

        # Verify content has workflow_dispatch
        new_content_decoded = base64.b64decode(call_kwargs['content']).decode("utf-8")
        self.assertIn("workflow_dispatch:", new_content_decoded)


    @patch("auto_coder.util.github_action.get_ghapi_client")
    @patch("auto_coder.util.github_action.GitHubClient")
    def test_trigger_workflow_dispatch_duplicate_fix(self, mock_gh_client_cls, mock_get_ghapi):
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        repo_name = "owner/repo"
        workflow_id = "ci.yml"
        ref = "feature-branch"

        # Mock create_workflow_dispatch to raise 422 first, then succeed
        mock_api.actions.create_workflow_dispatch.side_effect = [
            Exception("422 Unprocessable Entity"),  # First call fails
            None  # Second call succeeds
        ]

        # Mock get_content to return file with duplicate workflow_dispatch
        import base64
        # YAML with duplicate workflow_dispatch
        yaml_content = "name: CI\non:\n  workflow_dispatch:\n  workflow_dispatch:\n  push:\n    branches: [main]\n"
        encoded_content = base64.b64encode(yaml_content.encode("utf-8")).decode("utf-8")

        mock_api.repos.get_content.return_value = {
            "content": encoded_content,
            "sha": "sha123"
        }

        # Execute
        with patch("time.sleep"):
             result = trigger_workflow_dispatch(repo_name, workflow_id, ref)

        # Verify
        self.assertTrue(result)

        # Check create_or_update_file_contents called
        mock_api.repos.create_or_update_file_contents.assert_called_once()
        call_kwargs = mock_api.repos.create_or_update_file_contents.call_args[1]

        # Verify content has only one workflow_dispatch
        new_content_decoded = base64.b64decode(call_kwargs['content']).decode("utf-8")
        self.assertEqual(new_content_decoded.count("workflow_dispatch:"), 1)

if __name__ == "__main__":
    unittest.main()
