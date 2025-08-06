"""
Tests for automation engine functionality.
"""

import pytest
import json
import os
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from src.auto_coder.automation_engine import AutomationEngine


class TestAutomationEngine:
    """Test cases for AutomationEngine class."""
    
    def test_init(self, mock_github_client, mock_gemini_client, temp_reports_dir):
        """Test AutomationEngine initialization."""
        with patch('os.makedirs'):
            engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
            
            assert engine.github == mock_github_client
            assert engine.gemini == mock_gemini_client
            assert engine.dry_run is True
            assert engine.reports_dir == "reports"
    
    @patch('src.auto_coder.automation_engine.datetime')
    def test_run_success(self, mock_datetime, mock_github_client, mock_gemini_client, test_repo_name):
        """Test successful automation run."""
        # Setup
        mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T00:00:00"
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._process_issues = Mock(return_value=[{'issue': 'processed'}])
        engine._process_pull_requests = Mock(return_value=[{'pr': 'processed'}])
        engine._save_report = Mock()
        
        # Execute
        result = engine.run(test_repo_name)
        
        # Assert
        assert result['repository'] == test_repo_name
        assert result['dry_run'] is True
        assert result['issues_processed'] == [{'issue': 'processed'}]
        assert result['prs_processed'] == [{'pr': 'processed'}]
        assert len(result['errors']) == 0
        
        engine._process_issues.assert_called_once_with(test_repo_name)
        engine._process_pull_requests.assert_called_once_with(test_repo_name)
        engine._save_report.assert_called_once()
    
    def test_run_with_error(self, mock_github_client, mock_gemini_client, test_repo_name):
        """Test automation run with error."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._process_issues = Mock(side_effect=Exception("Test error"))
        engine._process_pull_requests = Mock(return_value=[])
        engine._save_report = Mock()
        
        # Execute
        result = engine.run(test_repo_name)
        
        # Assert
        assert result['repository'] == test_repo_name
        assert len(result['errors']) == 1
        assert "Test error" in result['errors'][0]
    
    def test_create_feature_issues_success(self, mock_github_client, mock_gemini_client, test_repo_name, sample_feature_suggestion):
        """Test successful feature issues creation."""
        # Setup
        mock_issue = Mock()
        mock_issue.number = 123
        mock_issue.html_url = "https://github.com/test/repo/issues/123"
        
        mock_github_client.create_issue.return_value = mock_issue
        mock_gemini_client.suggest_features.return_value = [sample_feature_suggestion]
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)
        engine._get_repository_context = Mock(return_value={'name': 'test-repo'})
        engine._save_report = Mock()
        
        # Execute
        result = engine.create_feature_issues(test_repo_name)
        
        # Assert
        assert len(result) == 1
        assert result[0]['number'] == 123
        assert result[0]['title'] == sample_feature_suggestion['title']
        
        mock_gemini_client.suggest_features.assert_called_once()
        mock_github_client.create_issue.assert_called_once()
    
    def test_create_feature_issues_dry_run(self, mock_github_client, mock_gemini_client, test_repo_name, sample_feature_suggestion):
        """Test feature issues creation in dry run mode."""
        # Setup
        mock_gemini_client.suggest_features.return_value = [sample_feature_suggestion]
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._get_repository_context = Mock(return_value={'name': 'test-repo'})
        engine._save_report = Mock()
        
        # Execute
        result = engine.create_feature_issues(test_repo_name)
        
        # Assert
        assert len(result) == 1
        assert result[0]['title'] == sample_feature_suggestion['title']
        assert result[0]['dry_run'] is True
        
        mock_gemini_client.suggest_features.assert_called_once()
        mock_github_client.create_issue.assert_not_called()
    
    def test_process_issues_success(self, mock_github_client, mock_gemini_client, sample_issue_data, sample_analysis_result):
        """Test successful issues processing."""
        # Setup
        mock_issue = Mock()
        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data
        mock_gemini_client.analyze_issue.return_value = sample_analysis_result
        mock_gemini_client.generate_solution.return_value = {'solution': 'test'}
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_issue_actions = Mock(return_value=['action1', 'action2'])
        
        # Execute
        result = engine._process_issues("test/repo")
        
        # Assert
        assert len(result) == 1
        assert result[0]['issue_data'] == sample_issue_data
        assert result[0]['analysis'] == sample_analysis_result
        assert result[0]['solution'] == {'solution': 'test'}
        assert result[0]['actions_taken'] == ['action1', 'action2']
        
        mock_github_client.get_open_issues.assert_called_once()
        mock_gemini_client.analyze_issue.assert_called_once_with(sample_issue_data)
        mock_gemini_client.generate_solution.assert_called_once()
    
    def test_process_issues_low_priority_no_solution(self, mock_github_client, mock_gemini_client, sample_issue_data):
        """Test processing low priority issues without solution generation."""
        # Setup
        mock_issue = Mock()
        low_priority_analysis = {'priority': 'low', 'category': 'question'}
        
        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data
        mock_gemini_client.analyze_issue.return_value = low_priority_analysis
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_issue_actions = Mock(return_value=[])
        
        # Execute
        result = engine._process_issues("test/repo")
        
        # Assert
        assert len(result) == 1
        assert result[0]['solution'] is None
        mock_gemini_client.generate_solution.assert_not_called()
    
    def test_process_pull_requests_success(self, mock_github_client, mock_gemini_client, sample_pr_data):
        """Test successful pull requests processing."""
        # Setup
        mock_pr = Mock()
        pr_analysis = {'category': 'feature', 'risk_level': 'low'}
        
        mock_github_client.get_open_pull_requests.return_value = [mock_pr]
        mock_github_client.get_pr_details.return_value = sample_pr_data
        mock_gemini_client.analyze_pull_request.return_value = pr_analysis
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_pr_actions = Mock(return_value=['pr_action'])
        
        # Execute
        result = engine._process_pull_requests("test/repo")
        
        # Assert
        assert len(result) == 1
        assert result[0]['pr_data'] == sample_pr_data
        assert result[0]['analysis'] == pr_analysis
        assert result[0]['actions_taken'] == ['pr_action']
        
        mock_github_client.get_open_pull_requests.assert_called_once()
        mock_gemini_client.analyze_pull_request.assert_called_once_with(sample_pr_data)
    
    def test_take_issue_actions_dry_run(self, mock_github_client, mock_gemini_client, sample_issue_data, sample_analysis_result):
        """Test issue actions in dry run mode."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._format_analysis_comment = Mock(return_value="Test comment")
        
        # Execute
        result = engine._take_issue_actions("test/repo", sample_issue_data, sample_analysis_result, None)
        
        # Assert
        assert len(result) == 1
        assert "[DRY RUN]" in result[0]
        mock_github_client.add_comment_to_issue.assert_not_called()
    
    def test_take_issue_actions_auto_close_duplicate(self, mock_github_client, mock_gemini_client, sample_issue_data):
        """Test auto-closing duplicate issues."""
        # Setup
        duplicate_analysis = {'category': 'duplicate', 'tags': []}
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)
        engine._format_analysis_comment = Mock(return_value="Test comment")
        
        # Execute
        result = engine._take_issue_actions("test/repo", sample_issue_data, duplicate_analysis, None)
        
        # Assert
        assert len(result) == 2
        assert "Added analysis comment" in result[0]
        assert "Auto-closed issue" in result[1]
        
        mock_github_client.add_comment_to_issue.assert_called_once()
        mock_github_client.close_issue.assert_called_once()
    
    def test_take_pr_actions_success(self, mock_github_client, mock_gemini_client, sample_pr_data):
        """Test PR actions execution."""
        # Setup
        pr_analysis = {'category': 'feature', 'risk_level': 'low'}
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)
        engine._format_pr_analysis_comment = Mock(return_value="PR comment")
        
        # Execute
        result = engine._take_pr_actions("test/repo", sample_pr_data, pr_analysis)
        
        # Assert
        assert len(result) == 1
        assert "Added analysis comment to PR" in result[0]
        mock_github_client.add_comment_to_issue.assert_called_once_with("test/repo", sample_pr_data['number'], "PR comment")
    
    def test_get_repository_context_success(self, mock_github_client, mock_gemini_client):
        """Test successful repository context retrieval."""
        # Setup
        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.description = "Test description"
        mock_repo.language = "Python"
        mock_repo.stargazers_count = 100
        mock_repo.forks_count = 20
        
        mock_github_client.get_repository.return_value = mock_repo
        mock_github_client.get_open_issues.return_value = []
        mock_github_client.get_open_pull_requests.return_value = []
        mock_github_client.get_issue_details.return_value = {}
        mock_github_client.get_pr_details.return_value = {}
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        
        # Execute
        result = engine._get_repository_context("test/repo")
        
        # Assert
        assert result['name'] == "test-repo"
        assert result['description'] == "Test description"
        assert result['language'] == "Python"
        assert result['stars'] == 100
        assert result['forks'] == 20
    
    def test_format_analysis_comment(self, mock_github_client, mock_gemini_client, sample_analysis_result):
        """Test analysis comment formatting."""
        # Setup
        solution = {
            'summary': 'Fix the bug',
            'steps': [{'step': 1, 'description': 'Update code'}]
        }
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        
        # Execute
        result = engine._format_analysis_comment(sample_analysis_result, solution)
        
        # Assert
        assert "🤖 Auto-Coder Analysis" in result
        assert sample_analysis_result['category'] in result
        assert sample_analysis_result['priority'] in result
        assert solution['summary'] in result
        assert "This analysis was generated automatically" in result
    
    def test_format_pr_analysis_comment(self, mock_github_client, mock_gemini_client):
        """Test PR analysis comment formatting."""
        # Setup
        pr_analysis = {
            'category': 'feature',
            'risk_level': 'low',
            'review_priority': 'medium',
            'summary': 'New feature implementation',
            'recommendations': [{'action': 'Review carefully'}],
            'potential_issues': ['None identified']
        }
        
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        
        # Execute
        result = engine._format_pr_analysis_comment(pr_analysis)
        
        # Assert
        assert "🤖 Auto-Coder PR Analysis" in result
        assert pr_analysis['category'] in result
        assert pr_analysis['risk_level'] in result
        assert pr_analysis['summary'] in result
        assert "This analysis was generated automatically" in result
    
    def test_format_feature_issue_body(self, mock_github_client, mock_gemini_client, sample_feature_suggestion):
        """Test feature issue body formatting."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        
        # Execute
        result = engine._format_feature_issue_body(sample_feature_suggestion)
        
        # Assert
        assert "## Feature Request" in result
        assert sample_feature_suggestion['description'] in result
        assert sample_feature_suggestion['rationale'] in result
        assert sample_feature_suggestion['priority'] in result
        assert "This feature request was generated automatically" in result
        
        # Check acceptance criteria formatting
        for criteria in sample_feature_suggestion['acceptance_criteria']:
            assert f"- [ ] {criteria}" in result
    
    @patch('builtins.open')
    @patch('json.dump')
    @patch('os.path.join')
    @patch('os.makedirs')
    def test_save_report_success(self, mock_makedirs, mock_join, mock_json_dump, mock_open, mock_github_client, mock_gemini_client):
        """Test successful report saving."""
        # Setup
        mock_join.return_value = "reports/test_report.json"
        mock_file = Mock()
        mock_open.return_value.__enter__.return_value = mock_file

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        test_data = {'test': 'data'}

        # Execute
        engine._save_report(test_data, "test_report")

        # Assert
        mock_open.assert_called_once()
        mock_json_dump.assert_called_once_with(test_data, mock_file, indent=2, ensure_ascii=False)

    def test_should_auto_merge_pr_low_risk_bugfix(self, mock_github_client, mock_gemini_client):
        """Test PR should be auto-merged for low-risk bugfix."""
        # Setup
        analysis = {
            'risk_level': 'low',
            'category': 'bugfix',
            'recommendations': [{'action': 'This PR looks good and can be merged safely'}]
        }
        pr_data = {
            'mergeable': True,
            'draft': False
        }

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is True

    def test_should_auto_merge_pr_high_risk(self, mock_github_client, mock_gemini_client):
        """Test PR should not be auto-merged for high-risk changes."""
        # Setup
        analysis = {
            'risk_level': 'high',
            'category': 'bugfix',
            'recommendations': [{'action': 'This PR can be merged'}]
        }
        pr_data = {
            'mergeable': True,
            'draft': False
        }

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is False

    def test_should_auto_merge_pr_draft(self, mock_github_client, mock_gemini_client):
        """Test PR should not be auto-merged if it's a draft."""
        # Setup
        analysis = {
            'risk_level': 'low',
            'category': 'bugfix',
            'recommendations': [{'action': 'This PR can be merged'}]
        }
        pr_data = {
            'mergeable': True,
            'draft': True
        }

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._should_auto_merge_pr(analysis, pr_data)

        # Assert
        assert result is False

    @patch('subprocess.run')
    @patch('os.path.exists')
    def test_run_pr_tests_success(self, mock_exists, mock_run, mock_github_client, mock_gemini_client):
        """Test successful PR test execution."""
        # Setup
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=0, stdout="All tests passed", stderr="")

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._run_pr_tests("test/repo", pr_data)

        # Assert
        assert result['success'] is True
        assert result['output'] == "All tests passed"
        mock_run.assert_called_once_with(
            ['bash', 'scripts/test.sh'],
            capture_output=True,
            text=True,
            timeout=600
        )

    @patch('subprocess.run')
    @patch('os.path.exists')
    def test_run_pr_tests_failure(self, mock_exists, mock_run, mock_github_client, mock_gemini_client):
        """Test PR test execution failure."""
        # Setup
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="Test failed: assertion error")

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._run_pr_tests("test/repo", pr_data)

        # Assert
        assert result['success'] is False
        assert result['errors'] == "Test failed: assertion error"
        assert result['return_code'] == 1

    def test_extract_important_errors(self, mock_github_client, mock_gemini_client):
        """Test error extraction from test output."""
        # Setup
        test_result = {
            'success': False,
            'output': 'Running tests...\nERROR: Test failed\nSome other output\nFAILED: assertion error\nMore output',
            'errors': 'ImportError: module not found'
        }

        engine = AutomationEngine(mock_github_client, mock_gemini_client)

        # Execute
        result = engine._extract_important_errors(test_result)

        # Assert
        assert 'ERROR: Test failed' in result
        assert 'FAILED: assertion error' in result
        assert 'ImportError: module not found' in result
