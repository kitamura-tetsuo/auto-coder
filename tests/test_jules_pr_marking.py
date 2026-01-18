
import pytest
from unittest.mock import Mock, patch, MagicMock
from auto_coder.automation_engine import AutomationEngine, AutomationConfig
from auto_coder.util.gh_cache import GitHubClient

class TestJulesPRMarking:

    @patch("auto_coder.automation_engine.get_ghapi_client")
    def test_mark_jules_pr_as_ready_with_fallback(self, mock_get_ghapi, mock_github_client):
        """Test marking Jules PR as ready, including fallback fetch and GraphQL call."""

        # Setup mocks
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        # Mock GhApi client for fallback fetch
        mock_ghapi_instance = Mock()
        mock_get_ghapi.return_value = mock_ghapi_instance

        # Mock PR data - initially Jules PR, Draft, missing node_id
        pr_data = {
            "number": 123,
            "title": "Jules PR",
            "body": "I started a Jules session to work on this issue. Session ID: 123", # Required for _is_jules_pr check
            "draft": True,
            "labels": [],
            "created_at": "2023-01-01T00:00:00Z",
            "head": {"ref": "jules-branch"},
            # "node_id": missing
        }

        # Prepare get_open_prs_json to return our PR
        mock_github_client.get_open_prs_json.return_value = [pr_data]

        # Mock fallback fetch response
        mock_ghapi_instance.pulls.get.return_value = {"node_id": "PR_NODE_ID_123"}

        # Execute the method that triggers the logic
        # _get_candidates is called internally. We need to call a method that calls it.
        # But _get_candidates is private. We can test it directly if we access it,
        # or we mock essential parts and call `engine.run` or similar?
        # Actually _get_candidates logic is what we want to test.
        # Let's verify if we can invoke it via public API or just call it directly for this unit test.
        # Python allows calling private methods.

        # patch the source modules directly since they are imported inside the function
        with patch("auto_coder.util.dependabot_timestamp.should_process_dependabot_pr", return_value=False), \
             patch("auto_coder.util.github_action.preload_github_actions_status"), \
             patch("auto_coder.pr_processor._is_jules_pr", return_value=True):

             candidates = engine._get_candidates("owner/repo")

        # Assertions

        # 1. Verify Fallback Fetch was called
        mock_ghapi_instance.pulls.get.assert_called_once_with("owner", "repo", 123)

        # 2. Verify GraphQL mutation was called on GitHubClient (Fix verification)
        mock_github_client.graphql_query.assert_called_once()
        args, kwargs = mock_github_client.graphql_query.call_args
        assert "mutation" in kwargs['query']
        assert kwargs['variables'] == {"id": "PR_NODE_ID_123"}

        # 3. Verify PR data was updated in place
        assert pr_data["node_id"] == "PR_NODE_ID_123"
        assert pr_data["draft"] is False

    @patch("auto_coder.automation_engine.get_ghapi_client")
    def test_mark_jules_pr_as_ready_no_fallback(self, mock_get_ghapi, mock_github_client):
        """Test marking Jules PR as ready when node_id is already present."""

        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        pr_data = {
            "number": 456,
            "title": "Jules PR",
            "body": "I started a Jules session...",
            "draft": True,
            "node_id": "EXISTING_NODE_ID",
            "labels": [],
            "created_at": "2023-01-01T00:00:00Z"
        }

        mock_github_client.get_open_prs_json.return_value = [pr_data]

        with patch("auto_coder.util.dependabot_timestamp.should_process_dependabot_pr", return_value=False), \
             patch("auto_coder.util.github_action.preload_github_actions_status"), \
             patch("auto_coder.pr_processor._is_jules_pr", return_value=True):

             engine._get_candidates("owner/repo")

        # 1. Verify Fallback Fetch was NOT called
        mock_get_ghapi.return_value.pulls.get.assert_not_called()

        # 2. Verify GraphQL mutation called with existing ID
        mock_github_client.graphql_query.assert_called_once()
        args, kwargs = mock_github_client.graphql_query.call_args
        assert kwargs['variables'] == {"id": "EXISTING_NODE_ID"}
