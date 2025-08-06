"""
Tests for automation engine functionality.
"""

import pytest
import json
import os
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from src.auto_coder.automation_engine import AutomationEngine, CommandExecutor, CommandResult, AutomationConfig


class TestAutomationEngine:
    """Test cases for AutomationEngine class."""
    
    def test_init(self, mock_github_client, mock_gemini_client, temp_reports_dir):
        """Test AutomationEngine initialization."""
        with patch('os.makedirs'):
            engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

            assert engine.github == mock_github_client
            assert engine.gemini == mock_gemini_client
            assert engine.dry_run is True
            assert engine.config.REPORTS_DIR == "reports"
            assert hasattr(engine, 'cmd')
    
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
    
    def test_take_issue_actions_dry_run(self, mock_github_client, mock_gemini_client, sample_issue_data):
        """Test issue actions in dry run mode."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Execute
        result = engine._take_issue_actions("test/repo", sample_issue_data)

        # Assert
        assert len(result) == 1
        assert "[DRY RUN]" in result[0]
        assert "123" in result[0]

    def test_apply_issue_actions_directly(self, mock_github_client, mock_gemini_client):
        """Test direct issue actions application using Gemini CLI."""
        # Setup
        mock_gemini_client._run_gemini_cli.return_value = "Analyzed the issue and added implementation. This is a valid bug report that has been fixed."

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        issue_data = {
            'number': 123,
            'title': 'Bug in login system',
            'body': 'The login system has a bug',
            'labels': [{'name': 'bug'}],
            'state': 'open',
            'user': {'login': 'testuser'}
        }

        # Execute
        with patch.object(engine, '_commit_changes', return_value="Committed changes"):
            result = engine._apply_issue_actions_directly('test/repo', issue_data)

        # Assert
        assert len(result) == 3
        assert "Gemini CLI analyzed and took action" in result[0]
        assert "Added analysis comment" in result[1]
        assert "Committed changes" in result[2]
    
    def test_take_pr_actions_success(self, mock_github_client, mock_gemini_client, sample_pr_data):
        """Test PR actions execution."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Execute
        result = engine._take_pr_actions("test/repo", sample_pr_data)

        # Assert
        assert len(result) == 1
        assert "[DRY RUN]" in result[0]
        assert "456" in result[0]
    
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
            timeout=3600
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

    @patch('subprocess.run')
    def test_check_github_actions_status_all_passed(self, mock_run, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check when all checks pass."""
        # Setup
        mock_run.return_value = Mock(
            returncode=0,
            stdout='✓ test-check\n✓ another-check'
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert result['success'] is True
        assert result['total_checks'] == 2
        assert len(result['failed_checks']) == 0

    @patch('subprocess.run')
    def test_check_github_actions_status_some_failed(self, mock_run, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check when some checks fail."""
        # Setup
        mock_run.return_value = Mock(
            returncode=0,
            stdout='✓ passing-check\n✗ failing-check\n- pending-check'
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert result['success'] is False
        assert result['total_checks'] == 3
        assert len(result['failed_checks']) == 2
        assert result['failed_checks'][0]['name'] == 'failing-check'
        assert result['failed_checks'][0]['conclusion'] == 'failure'
        assert result['failed_checks'][1]['name'] == 'pending-check'
        assert result['failed_checks'][1]['conclusion'] == 'pending'

    @patch('subprocess.run')
    def test_check_github_actions_status_tab_format_with_failures(self, mock_run, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check with tab-separated format and failures."""
        # Setup - simulating the actual output format from gh CLI
        mock_run.return_value = Mock(
            returncode=1,  # Non-zero because some checks failed
            stdout='test\tfail\t2m50s\thttps://github.com/example/repo/actions/runs/123\nformat\tpass\t27s\thttps://github.com/example/repo/actions/runs/124\nlink-pr-to-issue\tskipping\t0\thttps://github.com/example/repo/actions/runs/125'
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert result['success'] is False  # Should be False because 'test' failed
        assert result['total_checks'] == 3
        assert len(result['failed_checks']) == 1  # Only 'test' failed, 'skipping' doesn't count as failure
        assert result['failed_checks'][0]['name'] == 'test'
        assert result['failed_checks'][0]['conclusion'] == 'failure'
        assert result['failed_checks'][0]['details_url'] == 'https://github.com/example/repo/actions/runs/123'

    @patch('subprocess.run')
    def test_check_github_actions_status_tab_format_all_pass(self, mock_run, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check with tab-separated format and all passing."""
        # Setup
        mock_run.return_value = Mock(
            returncode=0,
            stdout='test\tpass\t2m50s\thttps://github.com/example/repo/actions/runs/123\nformat\tpass\t27s\thttps://github.com/example/repo/actions/runs/124\nlink-pr-to-issue\tskipping\t0\thttps://github.com/example/repo/actions/runs/125'
        )

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._check_github_actions_status("test/repo", pr_data)

        # Assert
        assert result['success'] is True  # Should be True because all required checks passed
        assert result['total_checks'] == 3
        assert len(result['failed_checks']) == 0  # No failed checks

    @patch('subprocess.run')
    def test_checkout_pr_branch_success(self, mock_run, mock_github_client, mock_gemini_client):
        """Test successful PR branch checkout."""
        # Setup
        mock_run.return_value = Mock(returncode=0, stdout="Switched to branch", stderr="")

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._checkout_pr_branch("test/repo", pr_data)

        # Assert
        assert result is True
        mock_run.assert_called_once_with(
            ['gh', 'pr', 'checkout', '123'],
            capture_output=True,
            text=True,
            timeout=120
        )

    @patch('subprocess.run')
    def test_checkout_pr_branch_failure(self, mock_run, mock_github_client, mock_gemini_client):
        """Test PR branch checkout failure."""
        # Setup
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="Branch not found")

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._checkout_pr_branch("test/repo", pr_data)

        # Assert
        assert result is False

    def test_apply_github_actions_fixes_directly(self, mock_github_client, mock_gemini_client):
        """Test direct GitHub Actions fixes application using Gemini CLI."""
        # Setup
        mock_gemini_client._run_gemini_cli.return_value = "Fixed the GitHub Actions issues by updating the test configuration"

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {
            'number': 123,
            'title': 'Fix test issue',
            'body': 'This PR fixes the test configuration'
        }
        github_logs = "Error: Test failed due to missing dependency"

        # Execute
        with patch.object(engine, '_commit_changes', return_value="Committed changes"):
            result = engine._apply_github_actions_fixes_directly(pr_data, github_logs)

        # Assert
        assert len(result) == 2
        assert "Gemini CLI applied GitHub Actions fixes" in result[0]
        assert "Committed changes" in result[1]

    def test_apply_local_test_fixes_directly(self, mock_github_client, mock_gemini_client):
        """Test direct local test fixes application using Gemini CLI."""
        # Setup
        mock_gemini_client._run_gemini_cli.return_value = "Fixed the local test issues by updating the import statements"

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {
            'number': 123,
            'title': 'Fix import issue',
            'body': 'This PR fixes the import statements'
        }
        error_summary = "ImportError: cannot import name 'helper' from 'utils'"

        # Execute
        with patch.object(engine, '_commit_changes', return_value="Committed changes"):
            result = engine._apply_local_test_fixes_directly(pr_data, error_summary)

        # Assert
        assert len(result) == 2
        assert "Gemini CLI applied local test fixes" in result[0]
        assert "Committed changes" in result[1]

    def test_format_direct_fix_comment(self, mock_github_client, mock_gemini_client):
        """Test direct fix comment formatting."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {
            'number': 123,
            'title': 'Fix GitHub Actions',
            'body': 'This PR fixes the CI issues'
        }
        github_logs = "Error: Test failed\nFailed to install dependencies\nBuild process failed"
        fix_actions = ["Fixed configuration", "Updated dependencies"]

        # Execute
        result = engine._format_direct_fix_comment(pr_data, github_logs, fix_actions)

        # Assert
        assert "Auto-Coder Applied GitHub Actions Fixes" in result
        assert "**PR:** #123 - Fix GitHub Actions" in result
        assert "Error: Test failed" in result
        assert "Fixed configuration" in result
        assert "Updated dependencies" in result

    @patch('subprocess.run')
    def test_commit_changes_success(self, mock_run, mock_github_client, mock_gemini_client):
        """Test successful git commit."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git add
            Mock(returncode=0, stdout="", stderr="")   # git commit
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        fix_suggestion = {'summary': 'Fix test issue'}

        # Execute
        result = engine._commit_changes(fix_suggestion)

        # Assert
        assert "Committed changes: Auto-Coder: Fix test issue" in result
        assert mock_run.call_count == 2

    def test_check_github_actions_status_in_progress(self, mock_github_client, mock_gemini_client):
        """Test GitHub Actions status check with in-progress checks."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Mock gh CLI output for in-progress checks
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stdout="test\tin_progress\t2m30s\turl1\nbuild\tpending\t1m45s\turl2"
            )

            # Execute
            result = engine._check_github_actions_status('test/repo', pr_data)

            # Assert
            assert result['success'] is False
            assert result['in_progress'] is True
            assert len(result['checks']) == 2

    @patch('subprocess.run')
    def test_update_with_main_branch_up_to_date(self, mock_run, mock_github_client, mock_gemini_client):
        """Test updating PR branch when already up to date."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="0", stderr="")   # git rev-list
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._update_with_main_branch('test/repo', pr_data)

        # Assert
        assert len(result) == 1
        assert "up to date with main branch" in result[0]

    @patch('subprocess.run')
    def test_update_with_main_branch_merge_success(self, mock_run, mock_github_client, mock_gemini_client):
        """Test successful main branch merge."""
        # Setup
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git fetch
            Mock(returncode=0, stdout="3", stderr=""),  # git rev-list
            Mock(returncode=0, stdout="", stderr=""),  # git merge
            Mock(returncode=0, stdout="", stderr="")   # git push
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._update_with_main_branch('test/repo', pr_data)

        # Assert
        assert len(result) == 3
        assert "3 commits behind main" in result[0]
        assert "Successfully merged main branch" in result[1]
        assert "Pushed updated branch" in result[2]


class TestCommandExecutor:
    """Test cases for CommandExecutor class."""

    @patch('subprocess.run')
    def test_run_command_success(self, mock_run):
        """Test successful command execution."""
        # Setup
        mock_run.return_value = Mock(returncode=0, stdout="success", stderr="")

        # Execute
        result = CommandExecutor.run_command(['echo', 'test'])

        # Assert
        assert result.success is True
        assert result.stdout == "success"
        assert result.stderr == ""
        assert result.returncode == 0

    @patch('subprocess.run')
    def test_run_command_failure(self, mock_run):
        """Test failed command execution."""
        # Setup
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="error")

        # Execute
        result = CommandExecutor.run_command(['false'])

        # Assert
        assert result.success is False
        assert result.stdout == ""
        assert result.stderr == "error"
        assert result.returncode == 1

    @patch('subprocess.run')
    def test_run_command_timeout(self, mock_run):
        """Test command timeout handling."""
        # Setup
        mock_run.side_effect = subprocess.TimeoutExpired(['sleep', '10'], 5)

        # Execute
        result = CommandExecutor.run_command(['sleep', '10'], timeout=5)

        # Assert
        assert result.success is False
        assert "timed out" in result.stderr
        assert result.returncode == -1

    def test_auto_timeout_detection(self):
        """Test automatic timeout detection based on command type."""
        # Test git command timeout
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            CommandExecutor.run_command(['git', 'status'])
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert kwargs['timeout'] == CommandExecutor.DEFAULT_TIMEOUTS['git']


class TestAutomationConfig:
    """Test cases for AutomationConfig class."""

    def test_default_values(self):
        """Test default configuration values."""
        config = AutomationConfig()

        assert config.REPORTS_DIR == "reports"
        assert config.TEST_SCRIPT_PATH == "scripts/test.sh"
        assert config.MAX_PR_DIFF_SIZE == 2000
        assert config.MAX_PROMPT_SIZE == 1000
        assert config.MAX_RESPONSE_SIZE == 200
        assert config.MAX_FIX_ATTEMPTS == 3
        assert config.MAIN_BRANCH == "main"
        assert config.MERGE_METHOD == "--squash"
        assert config.MERGE_AUTO is True
