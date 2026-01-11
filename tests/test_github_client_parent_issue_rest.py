
import pytest
from unittest.mock import MagicMock, patch
from src.auto_coder.github_client import GitHubClient




class TestGitHubClientParentIssueREST:
    @patch("src.auto_coder.github_client.get_ghapi_client")
    def test_get_parent_issue_details_rest(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details uses GhApi and proper endpoint."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api
        
        # Mock Parent Issue Object (Plain Dict)
        mock_parent_issue = {"number": 50, "title": "Parent Issue"}
        
        # api is called directly: api(path, verb=..., headers=...)
        mock_api.return_value = mock_parent_issue
        
        client = GitHubClient.get_instance("token")
        
        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)
        
        # Assert
        assert result is not None
        assert result["number"] == 50
        assert result["title"] == "Parent Issue"
        
        # Verify Call
        mock_api.assert_called_once_with(
            "/repos/owner/repo/issues/100/parent",
            verb='GET',
            headers={
                "X-GitHub-Api-Version": "2022-11-28",
                "Accept": "application/vnd.github+json"
            }
        )

    @patch("src.auto_coder.github_client.get_ghapi_client")
    @patch.object(GitHubClient, 'get_issue')
    def test_get_parent_issue_body_rest(self, mock_get_issue, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_body uses REST calls."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api
        
        # Mock Response for get_parent_issue_details
        mock_parent_issue = {"number": 50, "title": "Parent"}
        mock_api.return_value = mock_parent_issue
        
        # Mock Response for get_issue (parent)
        mock_parent_issue_full = MagicMock()
        mock_parent_issue_full.body = "This is parent body."
        mock_get_issue.return_value = mock_parent_issue_full
        
        client = GitHubClient.get_instance("token")
        
        # Execute
        body = client.get_parent_issue_body("owner/repo", 100)
        
        # Assert
        assert body == "This is parent body."
        
        # Verify calls
        mock_api.assert_called() 
        mock_get_issue.assert_called_with("owner/repo", 50) 

    @patch("src.auto_coder.github_client.get_ghapi_client")
    def test_get_parent_issue_no_parent(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details returns None when no parent exists (404 on both)."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api
        
        # 1. dedicated api() raises 404
        # 2. fallback api.issues.get() returns issue without parent
        
        mock_api.side_effect = Exception("HTTP 404: Not Found")
        mock_api.issues.get.return_value = {"number": 100, "title": "No Parent Issue"}
        
        client = GitHubClient.get_instance("token")
        
        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)
        
        # Assert
        assert result is None
        
        # Verify fallback was tried
        mock_api.issues.get.assert_called_once()


    @patch("src.auto_coder.github_client.get_ghapi_client")
    def test_get_parent_issue_wrapped(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details handles wrapped nested response."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api
        
        # Mock Wrapped Response: {"parent": {"number": 50, ...}}
        mock_response = {"parent": {"number": 50, "title": "Wrapper Parent"}}
        
        mock_api.side_effect = None
        mock_api.return_value = mock_response
        
        client = GitHubClient.get_instance("token")
        
        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)
        
        # Assert
        assert result is not None
        assert result["number"] == 50
        assert result["title"] == "Wrapper Parent"


    @patch("src.auto_coder.github_client.get_ghapi_client")
    def test_get_parent_issue_error(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details handles unexpected errors."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api
        
        # 1. dedicated api() raises error
        # 2. fallback api.issues.get() raises error
        
        mock_api.side_effect = Exception("Some other error")
        mock_api.issues.get.side_effect = Exception("Fallback Error")
        
        client = GitHubClient.get_instance("token")
        
        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)
        
        # Assert
        assert result is None

    @patch("src.auto_coder.github_client.get_ghapi_client")
    def test_get_parent_issue_fallback(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details falls back to issue details on 404 from dedicated endpoint."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api
        
        # Dedicated endpoint returns 404 dict
        # Since the code checks parent_issue.get("status") == "404"
        mock_response_dedicated = {"message": "Not Found", "status": "404"}
        
        # Fallback (issue details) returns issue with 'parent'
        mock_issue_details = {"number": 100, "title": "Child", "parent": {"number": 50, "title": "Fallback Parent"}}
        
        # Configure calls
        # 1. Dedicated endpoint call -> returns 404 response
        # 2. api.issues.get() call -> returns issue details
        
        # We Mock side_effect for api() call. 
        # But api.issues.get needs to be configured BEFORE the calls start?
        # Actually, when api() is called, it returns the mock_response_dedicated.
        # Then the code catches the 404 condition and calls api.issues.get().
        
        mock_api.side_effect = [mock_response_dedicated]
        mock_api.issues.get.return_value = mock_issue_details

        client = GitHubClient.get_instance("token")
        
        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)
        
        # Assert
        assert result is not None
        assert result["number"] == 50
        assert result["title"] == "Fallback Parent"
        
        # Verify both were called
        mock_api.assert_called() 
        mock_api.issues.get.assert_called_once()
