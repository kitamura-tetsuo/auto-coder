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

    @patch('src.auto_coder.automation_engine.datetime')
    def test_run_jules_mode_success(self, mock_datetime, mock_github_client, test_repo_name):
        """Test successful jules mode run."""
        # Setup
        mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T00:00:00"

        engine = AutomationEngine(mock_github_client, None, dry_run=True)  # No gemini client
        engine._process_issues_jules_mode = Mock(return_value=[{'issue': 'labeled'}])
        engine._save_report = Mock()

        # Execute
        result = engine.run_jules_mode(test_repo_name)

        # Assert
        assert result['repository'] == test_repo_name
        assert result['dry_run'] is True
        assert result['mode'] == 'jules'
        assert result['issues_processed'] == [{'issue': 'labeled'}]
        assert len(result['errors']) == 0

        engine._process_issues_jules_mode.assert_called_once_with(test_repo_name)
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

    def test_process_issues_jules_mode(self, mock_github_client):
        """Test processing issues in jules mode."""
        # Setup
        mock_issue = Mock()
        mock_issue.number = 1

        sample_issue_data = {
            'number': 1,
            'title': 'Test Issue',
            'labels': ['bug']  # No 'jules' label initially
        }

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data

        engine = AutomationEngine(mock_github_client, None, dry_run=False)  # No gemini client

        # Execute
        result = engine._process_issues_jules_mode("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]['issue_data'] == sample_issue_data
        assert len(result[0]['actions_taken']) == 1
        assert "Added 'jules' label to issue #1" in result[0]['actions_taken'][0]

        mock_github_client.get_open_issues.assert_called_once()
        mock_github_client.add_labels_to_issue.assert_called_once_with("test/repo", 1, ['jules'])

    def test_process_issues_jules_mode_already_labeled(self, mock_github_client):
        """Test processing issues in jules mode when jules label already exists."""
        # Setup
        mock_issue = Mock()
        mock_issue.number = 1

        sample_issue_data = {
            'number': 1,
            'title': 'Test Issue',
            'labels': ['bug', 'jules']  # Already has 'jules' label
        }

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = sample_issue_data

        engine = AutomationEngine(mock_github_client, None, dry_run=False)  # No gemini client

        # Execute
        result = engine._process_issues_jules_mode("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]['issue_data'] == sample_issue_data
        assert len(result[0]['actions_taken']) == 1
        assert "already has 'jules' label" in result[0]['actions_taken'][0]

        mock_github_client.get_open_issues.assert_called_once()
        # Should not call add_labels_to_issue since label already exists
        mock_github_client.add_labels_to_issue.assert_not_called()
    
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

    def test_process_pull_requests_two_loop_priority(self, mock_github_client, mock_gemini_client):
        """Test that PRs are processed in two loops: merge first, then fix."""
        # Setup - only one PR that passes Actions
        passing_pr_data = {'number': 1, 'title': 'Passing PR'}

        mock_pr1 = Mock()
        mock_pr1.number = 1

        mock_github_client.get_open_pull_requests.return_value = [mock_pr1]
        mock_github_client.get_pr_details.return_value = passing_pr_data

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Mock GitHub Actions status - PR passes
        engine._check_github_actions_status = Mock(return_value={'success': True})

        # Mock processing methods
        engine._process_pr_for_merge = Mock(return_value={
            'pr_data': passing_pr_data,
            'actions_taken': ['Successfully merged PR #1'],
            'priority': 'merge'
        })

        # Execute
        result = engine._process_pull_requests("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]['pr_data']['number'] == 1
        assert result[0]['priority'] == 'merge'
        assert 'Successfully merged' in result[0]['actions_taken'][0]

        # Verify method calls
        engine._process_pr_for_merge.assert_called_once_with("test/repo", passing_pr_data)

    def test_process_pull_requests_failing_actions(self, mock_github_client, mock_gemini_client):
        """Test that PRs with failing Actions are processed in second loop."""
        # Setup - only one PR that fails Actions
        failing_pr_data = {'number': 2, 'title': 'Failing PR'}

        mock_pr2 = Mock()
        mock_pr2.number = 2

        mock_github_client.get_open_pull_requests.return_value = [mock_pr2]
        mock_github_client.get_pr_details.return_value = failing_pr_data

        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)

        # Mock GitHub Actions status - PR fails
        engine._check_github_actions_status = Mock(return_value={'success': False})

        # Mock processing methods
        engine._process_pr_for_fixes = Mock(return_value={
            'pr_data': failing_pr_data,
            'actions_taken': ['Fixed PR #2'],
            'priority': 'fix'
        })

        # Execute
        result = engine._process_pull_requests("test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]['pr_data']['number'] == 2
        assert result[0]['priority'] == 'fix'

        # Verify method calls
        engine._process_pr_for_fixes.assert_called_once_with("test/repo", failing_pr_data)

    def test_process_pr_for_merge_success(self, mock_github_client, mock_gemini_client, sample_pr_data):
        """Test processing PR for merge when Actions are passing."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)
        engine._merge_pr = Mock(return_value=True)

        # Execute
        result = engine._process_pr_for_merge("test/repo", sample_pr_data)

        # Assert
        assert result['pr_data'] == sample_pr_data
        assert result['priority'] == 'merge'
        assert len(result['actions_taken']) == 1
        assert "Successfully merged" in result['actions_taken'][0]
        engine._merge_pr.assert_called_once_with("test/repo", sample_pr_data['number'], {})

    def test_process_pr_for_fixes_success(self, mock_github_client, mock_gemini_client, sample_pr_data):
        """Test processing PR for fixes when Actions are failing."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        engine._take_pr_actions = Mock(return_value=['Fixed issue'])

        # Execute
        result = engine._process_pr_for_fixes("test/repo", sample_pr_data)

        # Assert
        assert result['pr_data'] == sample_pr_data
        assert result['priority'] == 'fix'
        assert result['actions_taken'] == ['Fixed issue']
        engine._take_pr_actions.assert_called_once_with("test/repo", sample_pr_data)

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_merge_pr_with_conflict_resolution_success(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test PR merge with successful conflict resolution."""
        # Setup
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"
        config.MAIN_BRANCH = "main"
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # Mock PR data
        pr_data = {'number': 123, 'title': 'Test PR', 'body': 'Test description'}
        mock_github_client.get_pr_details_by_number.return_value = pr_data

        # Mock merge failure due to conflicts, then success after resolution
        mock_run_command.side_effect = [
            Mock(success=False, stderr="not mergeable: the merge commit cannot be cleanly created"),  # Initial merge fails
            Mock(success=True, stdout="", stderr=""),  # git reset --hard
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="", stderr=""),  # git merge --abort
            Mock(success=True, stdout="", stderr=""),  # gh pr checkout
            Mock(success=True, stdout="", stderr=""),  # git fetch
            Mock(success=True, stdout="", stderr=""),  # git merge (no conflicts)
            Mock(success=True, stdout="", stderr=""),  # git push
            Mock(success=True, stdout="Merged successfully", stderr="")  # Retry merge
        ]

        # Mock conflict resolution
        engine._get_merge_conflict_info = Mock(return_value="")
        engine._resolve_merge_conflicts_with_gemini = Mock(return_value=["Resolved conflicts"])

        # Execute
        result = engine._merge_pr("test/repo", 123, {})

        # Assert
        assert result is True
        assert mock_run_command.call_count == 9

        # Verify the sequence of commands
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert calls[0] == ['gh', 'pr', 'merge', '123', '--squash']  # Initial merge attempt
        assert calls[1] == ['git', 'reset', '--hard']  # Reset git state
        assert calls[2] == ['git', 'clean', '-fd']  # Clean untracked files
        assert calls[3] == ['git', 'merge', '--abort']  # Abort ongoing merge
        assert calls[4] == ['gh', 'pr', 'checkout', '123']  # Checkout PR
        assert calls[5] == ['git', 'fetch', 'origin', 'main']  # Fetch main
        assert calls[6] == ['git', 'merge', 'origin/main']  # Merge main
        assert calls[7] == ['git', 'push']  # Push changes
        assert calls[8] == ['gh', 'pr', 'merge', '123', '--squash']  # Retry merge

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_merge_pr_with_conflict_resolution_failure(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test PR merge with failed conflict resolution."""
        # Setup
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"
        config.MAIN_BRANCH = "main"
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # Mock merge failure due to conflicts, then checkout failure
        mock_run_command.side_effect = [
            Mock(success=False, stderr="not mergeable: the merge commit cannot be cleanly created"),  # Initial merge fails
            Mock(success=True, stdout="", stderr=""),  # git reset --hard
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="", stderr=""),  # git merge --abort
            Mock(success=False, stderr="Failed to checkout PR")  # Checkout fails
        ]

        # Execute
        result = engine._merge_pr("test/repo", 123, {})

        # Assert
        assert result is False
        assert mock_run_command.call_count == 5

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_resolve_pr_merge_conflicts_git_cleanup(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test that git cleanup commands are executed before PR checkout."""
        # Setup
        config = AutomationConfig()
        config.MAIN_BRANCH = "main"
        engine = AutomationEngine(mock_github_client, mock_gemini_client, config=config)

        # Mock PR data
        pr_data = {'number': 123, 'title': 'Test PR', 'body': 'Test description'}
        mock_github_client.get_pr_details_by_number.return_value = pr_data

        # Mock all commands to succeed
        mock_run_command.side_effect = [
            Mock(success=True, stdout="", stderr=""),  # git reset --hard
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="", stderr=""),  # git merge --abort
            Mock(success=True, stdout="", stderr=""),  # gh pr checkout
            Mock(success=True, stdout="", stderr=""),  # git fetch
            Mock(success=True, stdout="", stderr=""),  # git merge (no conflicts)
            Mock(success=True, stdout="", stderr=""),  # git push
        ]

        # Execute
        result = engine._resolve_pr_merge_conflicts("test/repo", 123)

        # Assert
        assert result is True
        assert mock_run_command.call_count == 7

        # Verify the sequence of commands includes git cleanup
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert calls[0] == ['git', 'reset', '--hard']  # Reset git state
        assert calls[1] == ['git', 'clean', '-fd']  # Clean untracked files
        assert calls[2] == ['git', 'merge', '--abort']  # Abort ongoing merge
        assert calls[3] == ['gh', 'pr', 'checkout', '123']  # Checkout PR
        assert calls[4] == ['git', 'fetch', 'origin', 'main']  # Fetch main
        assert calls[5] == ['git', 'merge', 'origin/main']  # Merge main
        assert calls[6] == ['git', 'push']  # Push changes

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
            'labels': ['bug'],
            'state': 'open',
            'author': 'testuser'
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

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_checkout_pr_branch_success(self, mock_run_command, mock_github_client, mock_gemini_client):
        """Test successful PR branch checkout."""
        # Setup
        mock_run_command.side_effect = [
            Mock(success=True, stdout="", stderr=""),  # git reset --hard HEAD
            Mock(success=True, stdout="", stderr=""),  # git clean -fd
            Mock(success=True, stdout="Switched to branch", stderr="")  # gh pr checkout
        ]

        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123}

        # Execute
        result = engine._checkout_pr_branch("test/repo", pr_data)

        # Assert
        assert result is True
        assert mock_run_command.call_count == 3

        # Verify the sequence of commands
        calls = [call[0][0] for call in mock_run_command.call_args_list]
        assert calls[0] == ['git', 'reset', '--hard', 'HEAD']
        assert calls[1] == ['git', 'clean', '-fd']
        assert calls[2] == ['gh', 'pr', 'checkout', '123']

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


class TestAutomationEngineExtended:
    """Extended test cases for AutomationEngine."""

    def test_handle_pr_merge_in_progress(self, mock_github_client, mock_gemini_client):
        """Test PR merge handling when GitHub Actions are in progress."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123, 'title': 'Test PR'}

        # Mock GitHub Actions in progress
        with patch.object(engine, '_check_github_actions_status') as mock_check:
            mock_check.return_value = {'success': False, 'in_progress': True, 'checks': []}

            # Execute
            result = engine._handle_pr_merge('test/repo', pr_data, {})

            # Assert
            assert len(result) == 1
            assert "still in progress" in result[0]
            assert "skipping to next PR" in result[0]

    def test_handle_pr_merge_success(self, mock_github_client, mock_gemini_client):
        """Test PR merge handling when GitHub Actions pass."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        pr_data = {'number': 123, 'title': 'Test PR'}

        # Mock GitHub Actions success
        with patch.object(engine, '_check_github_actions_status') as mock_check:
            mock_check.return_value = {'success': True, 'in_progress': False, 'checks': []}

            # Execute
            result = engine._handle_pr_merge('test/repo', pr_data, {})

            # Assert
            assert len(result) == 2
            assert "All GitHub Actions checks passed" in result[0]
            assert "[DRY RUN] Would merge" in result[1]

    def test_handle_pr_merge_with_integrated_fix(self, mock_github_client, mock_gemini_client):
        """Test PR merge handling with integrated GitHub Actions and local test fixing."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        pr_data = {'number': 123, 'title': 'Test PR'}
        failed_checks = [{'name': 'test', 'status': 'failed'}]

        # Mock GitHub Actions failure, checkout success, and up-to-date branch
        with patch.object(engine, '_check_github_actions_status') as mock_check, \
             patch.object(engine, '_checkout_pr_branch') as mock_checkout, \
             patch.object(engine, '_update_with_main_branch') as mock_update, \
             patch.object(engine, '_get_github_actions_logs') as mock_logs, \
             patch.object(engine, '_fix_pr_issues_with_testing') as mock_fix:

            mock_check.return_value = {'success': False, 'in_progress': False, 'failed_checks': failed_checks}
            mock_checkout.return_value = True
            mock_update.return_value = ["PR #123 is up to date with main branch"]
            mock_logs.return_value = "Test failed: assertion error"
            mock_fix.return_value = ["Applied GitHub Actions fix", "Local tests passed", "Committed and pushed fix"]

            # Execute
            result = engine._handle_pr_merge('test/repo', pr_data, {})

            # Assert
            assert any("up to date with main branch" in action for action in result)
            assert any("test failures are due to PR content" in action for action in result)
            mock_logs.assert_called_once_with('test/repo', failed_checks)
            mock_fix.assert_called_once_with('test/repo', pr_data, "Test failed: assertion error")

    def test_fix_pr_issues_with_testing_success(self, mock_github_client, mock_gemini_client):
        """Test integrated PR issue fixing with successful local tests."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        pr_data = {'number': 123, 'title': 'Test PR'}
        github_logs = "Test failed: assertion error"

        # Mock successful test after initial fix
        with patch.object(engine, '_apply_github_actions_fix') as mock_github_fix, \
             patch.object(engine, '_run_pr_tests') as mock_test:

            mock_github_fix.return_value = ["Applied GitHub Actions fix"]
            mock_test.return_value = {'success': True, 'output': 'All tests passed', 'errors': ''}

            # Execute
            result = engine._fix_pr_issues_with_testing('test/repo', pr_data, github_logs)

            # Assert
            assert any("Starting PR issue fixing" in action for action in result)
            assert any("Local tests passed on attempt 1" in action for action in result)
            assert any("[DRY RUN] Would commit and push fix" in action for action in result)
            mock_github_fix.assert_called_once()
            mock_test.assert_called_once()

    def test_fix_pr_issues_with_testing_retry(self, mock_github_client, mock_gemini_client):
        """Test integrated PR issue fixing with retry logic."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
        pr_data = {'number': 123, 'title': 'Test PR'}
        github_logs = "Test failed: assertion error"

        # Mock test failure then success
        with patch.object(engine, '_apply_github_actions_fix') as mock_github_fix, \
             patch.object(engine, '_run_pr_tests') as mock_test, \
             patch.object(engine, '_apply_local_test_fix') as mock_local_fix:

            mock_github_fix.return_value = ["Applied GitHub Actions fix"]
            # First test fails, second test passes
            mock_test.side_effect = [
                {'success': False, 'output': 'Test failed', 'errors': 'Error'},
                {'success': True, 'output': 'All tests passed', 'errors': ''}
            ]
            mock_local_fix.return_value = ["Applied local test fix"]

            # Execute
            result = engine._fix_pr_issues_with_testing('test/repo', pr_data, github_logs)

            # Assert
            assert any("Local tests failed on attempt 1" in action for action in result)
            assert any("Local tests passed on attempt 2" in action for action in result)
            mock_github_fix.assert_called_once()
            assert mock_test.call_count == 2
            mock_local_fix.assert_called_once()

    def test_checkout_pr_branch_force_cleanup(self, mock_github_client, mock_gemini_client):
        """Test PR branch checkout with force cleanup."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {'number': 123, 'title': 'Test PR'}

        # Mock successful force cleanup and checkout
        with patch.object(engine.cmd, 'run_command') as mock_cmd:
            # Mock git reset, git clean, and gh pr checkout success
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr=""),  # git reset --hard HEAD
                Mock(success=True, stdout="", stderr=""),  # git clean -fd
                Mock(success=True, stdout="", stderr="")   # gh pr checkout
            ]

            # Execute
            result = engine._checkout_pr_branch('test/repo', pr_data)

            # Assert
            assert result is True
            assert mock_cmd.call_count == 3
            mock_cmd.assert_any_call(['git', 'reset', '--hard', 'HEAD'])
            mock_cmd.assert_any_call(['git', 'clean', '-fd'])
            mock_cmd.assert_any_call(['gh', 'pr', 'checkout', '123'])

    def test_checkout_pr_branch_manual_fallback(self, mock_github_client, mock_gemini_client):
        """Test PR branch checkout with manual fallback."""
        # Setup
        engine = AutomationEngine(mock_github_client, mock_gemini_client)
        pr_data = {
            'number': 123,
            'title': 'Test PR',
            'head': {'ref': 'feature-branch'}
        }

        # Mock gh pr checkout failure, then manual success
        with patch.object(engine.cmd, 'run_command') as mock_cmd:
            # Mock git reset, git clean success, gh pr checkout failure, then manual success
            mock_cmd.side_effect = [
                Mock(success=True, stdout="", stderr=""),   # git reset --hard HEAD
                Mock(success=True, stdout="", stderr=""),   # git clean -fd
                Mock(success=False, stdout="", stderr="checkout failed"),  # gh pr checkout (fails)
                Mock(success=True, stdout="", stderr=""),   # git fetch (manual)
                Mock(success=True, stdout="", stderr="")    # git checkout -B (manual)
            ]

            # Execute
            result = engine._checkout_pr_branch('test/repo', pr_data)

            # Assert
            assert result is True
            assert mock_cmd.call_count == 5
            mock_cmd.assert_any_call(['git', 'fetch', 'origin', 'pull/123/head:feature-branch'])
            mock_cmd.assert_any_call(['git', 'checkout', '-B', 'feature-branch'])
