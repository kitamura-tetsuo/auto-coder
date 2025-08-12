"""
End-to-end tests for Auto-Coder.
"""

import pytest
import os
import json
import tempfile
from unittest.mock import Mock, patch, MagicMock
from click.testing import CliRunner

from src.auto_coder.cli import main
from src.auto_coder.github_client import GitHubClient
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.automation_engine import AutomationEngine


class TestE2E:
    """End-to-end test cases for Auto-Coder."""
    
    @pytest.fixture
    def temp_reports_dir(self):
        """Create temporary reports directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = os.path.join(temp_dir, "reports")
            os.makedirs(reports_dir)
            yield reports_dir
    
    @pytest.fixture
    def mock_github_responses(self):
        """Mock GitHub API responses."""
        # Mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_issue.title = "Test Bug Report"
        mock_issue.body = "This is a test bug that needs to be fixed"
        mock_issue.state = "open"
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_label2 = Mock()
        mock_label2.name = "high-priority"
        mock_issue.labels = [mock_label1, mock_label2]
        mock_assignee = Mock()
        mock_assignee.login = "testuser"
        mock_issue.assignees = [mock_assignee]
        mock_issue.created_at = Mock()
        mock_issue.created_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.updated_at = Mock()
        mock_issue.updated_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.html_url = "https://github.com/test/repo/issues/1"
        mock_issue.user = Mock(login="testuser")
        mock_issue.comments = 0
        mock_issue.pull_request = None
        
        # Mock PR
        mock_pr = Mock()
        mock_pr.number = 2
        mock_pr.title = "Fix test bug"
        mock_pr.body = "This PR fixes the test bug"
        mock_pr.state = "open"
        mock_pr_label = Mock()
        mock_pr_label.name = "bugfix"
        mock_pr.labels = [mock_pr_label]
        mock_pr_assignee = Mock()
        mock_pr_assignee.login = "testuser"
        mock_pr.assignees = [mock_pr_assignee]
        mock_pr.created_at = Mock()
        mock_pr.created_at.isoformat.return_value = "2024-01-01T01:00:00Z"
        mock_pr.updated_at = Mock()
        mock_pr.updated_at.isoformat.return_value = "2024-01-01T01:00:00Z"
        mock_pr.html_url = "https://github.com/test/repo/pull/2"
        mock_pr.user = Mock(login="testuser")
        mock_pr.head = Mock(ref="fix-bug")
        mock_pr.base = Mock(ref="main")
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.comments = 0
        mock_pr.review_comments = 0
        mock_pr.commits = 1
        mock_pr.additions = 10
        mock_pr.deletions = 2
        mock_pr.changed_files = 1
        
        # Mock repository
        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.description = "Test repository for e2e testing"
        mock_repo.language = "Python"
        mock_repo.stargazers_count = 50
        mock_repo.forks_count = 10
        
        return {
            'issue': mock_issue,
            'pr': mock_pr,
            'repo': mock_repo
        }
    
    @pytest.fixture
    def mock_gemini_responses(self):
        """Mock Gemini API responses."""
        return {
            'issue_analysis': {
                "category": "bug",
                "priority": "high",
                "complexity": "moderate",
                "estimated_effort": "days",
                "tags": ["backend", "critical"],
                "recommendations": [
                    {
                        "action": "Investigate the root cause of the bug",
                        "rationale": "Understanding the cause will help prevent similar issues"
                    },
                    {
                        "action": "Add unit tests to cover the bug scenario",
                        "rationale": "Tests will prevent regression"
                    }
                ],
                "related_components": ["api", "database"],
                "summary": "Critical bug affecting user authentication flow"
            },
            'pr_analysis': {
                "category": "bugfix",
                "risk_level": "low",
                "review_priority": "high",
                "estimated_review_time": "hours",
                "recommendations": [
                    {
                        "action": "Review the fix implementation carefully",
                        "rationale": "Bug fixes need thorough review"
                    }
                ],
                "potential_issues": ["None identified"],
                "summary": "Bug fix for authentication flow issue"
            },
            'feature_suggestions': [
                {
                    "title": "Add user profile management",
                    "description": "Allow users to manage their profile information including avatar, bio, and preferences",
                    "rationale": "Users need to be able to customize their experience and maintain their profile",
                    "priority": "medium",
                    "complexity": "moderate",
                    "estimated_effort": "weeks",
                    "labels": ["enhancement", "user-experience"],
                    "acceptance_criteria": [
                        "Users can upload and change their avatar",
                        "Users can edit their bio and personal information",
                        "Users can set notification preferences"
                    ]
                }
            ],
            'solution': {
                "solution_type": "code_fix",
                "summary": "Fix authentication flow by updating session validation logic",
                "steps": [
                    {
                        "step": 1,
                        "description": "Update session validation in auth middleware",
                        "commands": ["git checkout -b fix-auth-session"]
                    },
                    {
                        "step": 2,
                        "description": "Add proper error handling for expired sessions",
                        "commands": ["python -m pytest tests/test_auth.py"]
                    }
                ],
                "code_changes": [
                    {
                        "file": "src/auth/middleware.py",
                        "action": "modify",
                        "description": "Update session validation logic",
                        "code": "def validate_session(session_token):\n    if not session_token or is_expired(session_token):\n        raise AuthenticationError('Invalid or expired session')\n    return True"
                    }
                ],
                "testing_strategy": "Add unit tests for session validation and integration tests for auth flow",
                "risks": ["Potential breaking changes to existing auth flow"]
            }
        }
    
    @patch('src.auto_coder.automation_engine.os.makedirs')
    @patch('src.auto_coder.github_client.Github')
    @patch('src.auto_coder.gemini_client.genai')
    def test_full_automation_workflow_dry_run(self, mock_genai, mock_github_class, mock_makedirs, 
                                            mock_github_responses, mock_gemini_responses, temp_reports_dir):
        """Test complete automation workflow in dry-run mode."""
        # Setup GitHub mocks
        mock_github = Mock()
        mock_repo = mock_github_responses['repo']
        mock_issue = mock_github_responses['issue']
        mock_pr = mock_github_responses['pr']
        
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issues.return_value = [mock_issue]
        mock_repo.get_pulls.return_value = [mock_pr]
        mock_github_class.return_value = mock_github
        
        # Setup Gemini mocks
        mock_model = Mock()
        mock_genai.GenerativeModel.return_value = mock_model
        
        # Mock responses for different calls
        mock_responses = [
            Mock(text=json.dumps(mock_gemini_responses['issue_analysis'])),
            Mock(text=json.dumps(mock_gemini_responses['solution'])),
            Mock(text=json.dumps(mock_gemini_responses['pr_analysis']))
        ]
        mock_model.generate_content.side_effect = mock_responses
        
        # Create automation engine
        github_client = GitHubClient("test_token")
        gemini_client = GeminiClient("test_key")
        
        automation_engine = AutomationEngine(github_client, gemini_client, dry_run=True)
        automation_engine.reports_dir = temp_reports_dir

        # Run automation
        result = automation_engine.run("test/repo")
        
        # Verify results
        assert result['repository'] == "test/repo"
        assert result['dry_run'] is True
        assert len(result['issues_processed']) == 1
        assert len(result['prs_processed']) == 1
        assert len(result['errors']) == 0
        
        # Verify issue processing
        issue_result = result['issues_processed'][0]
        assert issue_result['issue_data']['number'] == 1
        assert issue_result['analysis']['category'] == 'bug'
        assert issue_result['analysis']['priority'] == 'high'
        assert issue_result['solution']['solution_type'] == 'code_fix'
        assert len(issue_result['actions_taken']) > 0
        assert any("[DRY RUN]" in action for action in issue_result['actions_taken'])
        
        # Verify PR processing
        pr_result = result['prs_processed'][0]
        assert pr_result['pr_data']['number'] == 2
        assert pr_result['analysis']['category'] == 'bugfix'
        assert pr_result['analysis']['risk_level'] == 'low'
        assert len(pr_result['actions_taken']) > 0
        assert any("[DRY RUN]" in action for action in pr_result['actions_taken'])
    
    @patch('src.auto_coder.automation_engine.os.makedirs')
    @patch('src.auto_coder.github_client.Github')
    @patch('src.auto_coder.gemini_client.genai')
    def test_feature_suggestion_workflow(self, mock_genai, mock_github_class, mock_makedirs,
                                       mock_github_responses, mock_gemini_responses, temp_reports_dir):
        """Test feature suggestion workflow."""
        # Setup GitHub mocks
        mock_github = Mock()
        mock_repo = mock_github_responses['repo']
        mock_issue = mock_github_responses['issue']
        mock_pr = mock_github_responses['pr']
        
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issues.return_value = [mock_issue]
        mock_repo.get_pulls.return_value = [mock_pr]
        mock_github_class.return_value = mock_github
        
        # Setup Gemini mocks
        mock_model = Mock()
        mock_response = Mock(text=json.dumps(mock_gemini_responses['feature_suggestions']))
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model
        
        # Create automation engine
        github_client = GitHubClient("test_token")
        gemini_client = GeminiClient("test_key")
        
        automation_engine = AutomationEngine(github_client, gemini_client, dry_run=True)
        automation_engine.reports_dir = temp_reports_dir

        # Run feature suggestion
        result = automation_engine.create_feature_issues("test/repo")
        
        # Verify results
        assert len(result) == 1
        assert result[0]['title'] == "Add user profile management"
        assert result[0]['dry_run'] is True
        
        # Verify that Gemini was called for feature suggestions
        mock_model.generate_content.assert_called_once()
    
    def test_cli_integration_process_issues(self, mock_github_responses, mock_gemini_responses):
        """Test CLI integration for process-issues command."""
        runner = CliRunner()
        
        with patch('src.auto_coder.cli.GitHubClient') as mock_github_client_class, \
             patch('src.auto_coder.cli.GeminiClient') as mock_gemini_client_class, \
             patch('src.auto_coder.cli.AutomationEngine') as mock_automation_engine_class:
            
            # Setup mocks
            mock_github_client = Mock()
            mock_gemini_client = Mock()
            mock_automation_engine = Mock()
            
            mock_github_client_class.return_value = mock_github_client
            mock_gemini_client_class.return_value = mock_gemini_client
            mock_automation_engine_class.return_value = mock_automation_engine
            
            mock_automation_engine.run.return_value = {
                'repository': 'test/repo',
                'issues_processed': 1,
                'prs_processed': 1,
                'errors': []
            }
            
            # Execute CLI command
            result = runner.invoke(main, [
                'process-issues',
                '--repo', 'test/repo',
                '--github-token', 'test_token',
                '--backend', 'gemini',
                '--gemini-api-key', 'test_key',
                '--dry-run'
            ])

            # Verify CLI execution
            assert result.exit_code == 0
            assert "Processing repository: test/repo" in result.output
            assert "Dry run mode: True" in result.output
            
            # Verify that automation engine was called
            mock_automation_engine.run.assert_called_once_with('test/repo')

    def test_cli_integration_create_feature_issues(self, mock_github_responses, mock_gemini_responses):
        """Test CLI integration for create-feature-issues command."""
        runner = CliRunner()
        
        with patch('src.auto_coder.cli.GitHubClient') as mock_github_client_class, \
             patch('src.auto_coder.cli.GeminiClient') as mock_gemini_client_class, \
             patch('src.auto_coder.cli.AutomationEngine') as mock_automation_engine_class:
            
            # Setup mocks
            mock_github_client = Mock()
            mock_gemini_client = Mock()
            mock_automation_engine = Mock()
            
            mock_github_client_class.return_value = mock_github_client
            mock_gemini_client_class.return_value = mock_gemini_client
            mock_automation_engine_class.return_value = mock_automation_engine
            
            mock_automation_engine.create_feature_issues.return_value = [
                {'title': 'New Feature', 'dry_run': True}
            ]
            
            # Execute CLI command
            result = runner.invoke(main, [
                'create-feature-issues',
                '--repo', 'test/repo',
                '--github-token', 'test_token',
                '--gemini-api-key', 'test_key'
            ])
            
            # Verify CLI execution
            assert result.exit_code == 0
            assert "Analyzing repository for feature opportunities: test/repo" in result.output
            
            # Verify that automation engine was called
            mock_automation_engine.create_feature_issues.assert_called_once_with('test/repo')
