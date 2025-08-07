"""
Pytest configuration and fixtures for Auto-Coder tests.
"""

import pytest
import os
from unittest.mock import Mock, MagicMock
from typing import Dict, Any

from src.auto_coder.github_client import GitHubClient
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.automation_engine import AutomationEngine


@pytest.fixture
def mock_github_token():
    """Mock GitHub token for testing."""
    return "test_github_token"


@pytest.fixture
def mock_gemini_api_key():
    """Mock Gemini API key for testing."""
    return "test_gemini_api_key"


@pytest.fixture
def mock_github_client(mock_github_token):
    """Mock GitHub client for testing."""
    client = Mock(spec=GitHubClient)
    client.token = mock_github_token
    return client


@pytest.fixture
def mock_gemini_client(mock_gemini_api_key):
    """Mock Gemini client for testing."""
    client = Mock(spec=GeminiClient)
    client.api_key = mock_gemini_api_key
    client.model_name = "gemini-2.5-pro"
    return client


@pytest.fixture
def mock_automation_engine(mock_github_client, mock_gemini_client):
    """Mock automation engine for testing."""
    engine = Mock(spec=AutomationEngine)
    engine.github = mock_github_client
    engine.gemini = mock_gemini_client
    engine.dry_run = True
    return engine


@pytest.fixture
def sample_issue_data():
    """Sample issue data for testing."""
    return {
        'number': 123,
        'title': 'Test Issue',
        'body': 'This is a test issue description',
        'state': 'open',
        'labels': ['bug', 'high-priority'],
        'assignees': ['testuser'],
        'created_at': '2024-01-01T00:00:00Z',
        'updated_at': '2024-01-01T00:00:00Z',
        'url': 'https://github.com/test/repo/issues/123',
        'author': 'testuser',
        'comments_count': 2
    }


@pytest.fixture
def sample_pr_data():
    """Sample PR data for testing."""
    return {
        'number': 456,
        'title': 'Test Pull Request',
        'body': 'This is a test pull request description',
        'state': 'open',
        'labels': ['feature'],
        'assignees': ['testuser'],
        'created_at': '2024-01-01T00:00:00Z',
        'updated_at': '2024-01-01T00:00:00Z',
        'url': 'https://github.com/test/repo/pull/456',
        'author': 'testuser',
        'head_branch': 'feature-branch',
        'base_branch': 'main',
        'mergeable': True,
        'draft': False,
        'comments_count': 1,
        'review_comments_count': 0,
        'commits_count': 3,
        'additions': 50,
        'deletions': 10,
        'changed_files': 2
    }


@pytest.fixture
def sample_analysis_result():
    """Sample analysis result for testing."""
    return {
        'category': 'bug',
        'priority': 'high',
        'complexity': 'moderate',
        'estimated_effort': 'days',
        'tags': ['backend', 'api'],
        'recommendations': [
            {
                'action': 'Fix the API endpoint',
                'rationale': 'The endpoint is returning incorrect data'
            }
        ],
        'related_components': ['api', 'database'],
        'summary': 'API endpoint returning incorrect data'
    }


@pytest.fixture
def sample_feature_suggestion():
    """Sample feature suggestion for testing."""
    return {
        'title': 'Add user authentication',
        'description': 'Implement user authentication system with JWT tokens',
        'rationale': 'Users need to be able to securely access their data',
        'priority': 'high',
        'complexity': 'complex',
        'estimated_effort': 'weeks',
        'labels': ['enhancement', 'security'],
        'acceptance_criteria': [
            'Users can register with email and password',
            'Users can login and receive JWT token',
            'Protected routes require valid JWT token'
        ]
    }


@pytest.fixture
def test_repo_name():
    """Test repository name."""
    return "test-owner/test-repo"


@pytest.fixture
def temp_reports_dir(tmp_path):
    """Temporary directory for test reports."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    return str(reports_dir)
