
import pytest
from unittest.mock import MagicMock, patch
from src.auto_coder.util.gh_cache import GitHubClient

class TestGitHubClientTimelineREST:
    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_get_linked_prs_rest(self, mock_get_caching, mock_github_token):
        """Test get_linked_prs uses REST timeline events."""
        # Setup
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        # Mock Response: Timeline with connected and cross-referenced events
        # We simulate a mix of events to test filtering
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"event": "commented", "id": 1},
            {
                "event": "connected",
                "source": {
                    "issue": {
                        "number": 101,
                        "pull_request": {"url": "..."}
                    }
                }
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 102,
                        "pull_request": {"url": "..."}
                    }
                }
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 55 # Not a PR (maybe another issue ref)
                        # No 'pull_request' key
                    }
                }
            }
        ]
        mock_client.get.return_value = mock_response

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        # Execute
        result = client.get_linked_prs("owner/repo", 1)

        # Assert
        assert set(result) == {101, 102}
        assert 55 not in result

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_verify_pr_closes_issue_rest(self, mock_get_caching, mock_github_token):
        """Test verify_pr_closes_issue uses REST timeline connected events."""
        # Setup
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        # Mock Response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "event": "connected",
                "source": {
                    "issue": {
                        "number": 101,
                        "pull_request": {}
                    }
                }
            }
        ]
        mock_client.get.return_value = mock_response

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        # Execute & Assert
        assert client.verify_pr_closes_issue("owner/repo", 101, 1) is True
        assert client.verify_pr_closes_issue("owner/repo", 999, 1) is False
