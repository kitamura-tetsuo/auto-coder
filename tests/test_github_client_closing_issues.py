import pytest
from unittest.mock import MagicMock, patch
from auto_coder.util.gh_cache import GitHubClient

@pytest.fixture
def mock_github_client():
    client = GitHubClient(token="fake_token")
    client.graphql_query = MagicMock()
    return client

def test_get_pr_closing_issues_success(mock_github_client):
    # Mock response data
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": [
                            {"number": 123},
                            {"number": 456}
                        ]
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    repo_name = "owner/repo"
    pr_number = 789

    closing_issues = mock_github_client.get_pr_closing_issues(repo_name, pr_number)

    # Check that graphql_query was called with correct arguments
    mock_github_client.graphql_query.assert_called_once()
    args, kwargs = mock_github_client.graphql_query.call_args
    assert "query" in args[0]
    assert args[1] == {
        "owner": "owner",
        "name": "repo",
        "number": pr_number
    }

    # Check result
    assert closing_issues == [123, 456]

def test_get_pr_closing_issues_none_found(mock_github_client):
    # Mock empty nodes
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": []
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    closing_issues = mock_github_client.get_pr_closing_issues("owner/repo", 789)
    assert closing_issues == []

def test_get_pr_closing_issues_api_failure(mock_github_client):
    # Mock exception
    mock_github_client.graphql_query.side_effect = Exception("API Error")

    closing_issues = mock_github_client.get_pr_closing_issues("owner/repo", 789)
    assert closing_issues == []
