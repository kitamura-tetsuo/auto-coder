#!/usr/bin/env python3
"""
Test script for the new GitHub Actions history fallback functionality.
Tests the enhanced _check_github_actions_status function with historical fallback.
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch
import sys

# Add src to path to import auto_coder modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import _check_github_actions_status, _check_github_actions_status_from_history


def test_github_actions_history_fallback_enabled():
    """Test that historical fallback works when enabled in config."""
    print("Testing GitHub Actions history fallback (enabled)...")
    
    # Create config with fallback enabled
    config = AutomationConfig()
    config.ENABLE_ACTIONS_HISTORY_FALLBACK = True
    
    # Mock PR data
    pr_data = {
        "number": 123,
        "head": {"ref": "feature-branch"}
    }
    
    # Mock the gh command to simulate failed current checks
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Failed to get checks: some other error"
    
    with patch('auto_coder.pr_processor.cmd.run_command', return_value=mock_result):
        with patch('auto_coder.pr_processor._check_github_actions_status_from_history') as mock_fallback:
            # Mock the fallback function to return a successful result
            mock_fallback.return_value = {
                "success": True,
                "in_progress": False,
                "checks": [],
                "failed_checks": [],
                "total_checks": 0,
                "historical_fallback": True
            }
            
            result = _check_github_actions_status("test/repo", pr_data, config)
            
            # Verify that fallback was called
            mock_fallback.assert_called_once_with("test/repo", pr_data, config)
            
            # Verify the result structure
            assert result["success"] == True
            assert result["historical_fallback"] == True
            print("✓ Historical fallback was called when enabled")
    

def test_github_actions_history_fallback_disabled():
    """Test that historical fallback is not used when disabled in config."""
    print("Testing GitHub Actions history fallback (disabled)...")
    
    # Create config with fallback disabled
    config = AutomationConfig()
    config.ENABLE_ACTIONS_HISTORY_FALLBACK = False
    
    # Mock PR data
    pr_data = {
        "number": 123,
        "head": {"ref": "feature-branch"}
    }
    
    # Mock the gh command to simulate failed current checks
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Failed to get checks: some other error"
    
    with patch('auto_coder.pr_processor.cmd.run_command', return_value=mock_result):
        with patch('auto_coder.pr_processor._check_github_actions_status_from_history') as mock_fallback:
            # Mock the fallback function (should not be called)
            mock_fallback.return_value = {
                "success": True,
                "in_progress": False,
                "checks": [],
                "failed_checks": [],
                "total_checks": 0,
                "historical_fallback": True
            }
            
            result = _check_github_actions_status("test/repo", pr_data, config)
            
            # Verify that fallback was NOT called
            mock_fallback.assert_not_called()
            
            # Verify the result indicates failure (no fallback)
            assert result["success"] == False
            assert "historical_fallback" not in result
            print("✓ Historical fallback was NOT called when disabled")


def test_check_github_actions_status_from_history():
    """Test the new _check_github_actions_status_from_history function."""
    print("Testing _check_github_actions_status_from_history function...")
    
    config = AutomationConfig()
    
    # Mock PR data
    pr_data = {
        "number": 123,
        "head": {"ref": "feature-branch"}
    }
    
    # Mock successful API responses
    run_list_result = MagicMock()
    run_list_result.success = True
    run_list_result.stdout = json.dumps([
        {
            "databaseId": 12345,
            "headBranch": "feature-branch", 
            "conclusion": "success",
            "createdAt": "2025-11-03T04:30:00Z",
            "status": "completed"
        }
    ])
    
    jobs_result = MagicMock()
    jobs_result.returncode = 0
    jobs_result.stdout = json.dumps({
        "jobs": [
            {
                "databaseId": 67890,
                "name": "CI",
                "conclusion": "success",
                "status": "completed"
            }
        ]
    })
    
    with patch('auto_coder.pr_processor.cmd.run_command') as mock_cmd:
        # First call for run list, second call for jobs
        mock_cmd.side_effect = [run_list_result, jobs_result]
        
        result = _check_github_actions_status_from_history("test/repo", pr_data, config)
        
        # Verify the result structure
        assert result["success"] == True
        assert result["in_progress"] == False
        assert result["historical_fallback"] == True
        assert result["source"] == "historical_runs"
        assert result["total_checks"] == 1
        assert len(result["checks"]) == 1
        assert len(result["failed_checks"]) == 0
        
        # Verify the check details
        check = result["checks"][0]
        assert check["name"] == "CI"
        assert check["state"] == "completed"
        assert check["conclusion"] == "success"
        assert "actions/runs/12345/job/67890" in check["details_url"]
        
        print("✓ Historical status check returned correct structure")


def test_check_github_actions_status_from_history_failure():
    """Test the new _check_github_actions_status_from_history function with failures."""
    print("Testing _check_github_actions_status_from_history function (with failures)...")
    
    config = AutomationConfig()
    
    # Mock PR data
    pr_data = {
        "number": 123,
        "head": {"ref": "feature-branch"}
    }
    
    # Mock API responses with failures
    run_list_result = MagicMock()
    run_list_result.success = True
    run_list_result.stdout = json.dumps([
        {
            "databaseId": 12345,
            "headBranch": "feature-branch", 
            "conclusion": "failure",
            "createdAt": "2025-11-03T04:30:00Z",
            "status": "completed"
        }
    ])
    
    jobs_result = MagicMock()
    jobs_result.returncode = 0
    jobs_result.stdout = json.dumps({
        "jobs": [
            {
                "databaseId": 67890,
                "name": "CI",
                "conclusion": "failure",
                "status": "completed"
            }
        ]
    })
    
    with patch('auto_coder.pr_processor.cmd.run_command') as mock_cmd:
        # First call for run list, second call for jobs
        mock_cmd.side_effect = [run_list_result, jobs_result]
        
        result = _check_github_actions_status_from_history("test/repo", pr_data, config)
        
        # Verify the result indicates failure
        assert result["success"] == False
        assert result["historical_fallback"] == True
        assert result["total_checks"] == 1
        assert len(result["failed_checks"]) == 1
        
        # Verify the failed check details
        failed_check = result["failed_checks"][0]
        assert failed_check["name"] == "CI"
        assert failed_check["conclusion"] == "failure"
        
        print("✓ Historical status check correctly identified failures")


def test_config_default_values():
    """Test that the new config option has the correct default value."""
    print("Testing config default values...")
    
    config = AutomationConfig()
    
    # Verify default values
    assert config.ENABLE_ACTIONS_HISTORY_FALLBACK == True
    assert config.SEARCH_GITHUB_ACTIONS_HISTORY == True
    
    print("✓ Configuration defaults are correct")


def main():
    """Run all tests."""
    print("Running GitHub Actions History Fallback Tests...")
    print("=" * 60)
    
    try:
        test_config_default_values()
        test_github_actions_history_fallback_enabled()
        test_github_actions_history_fallback_disabled()
        test_check_github_actions_status_from_history()
        test_check_github_actions_status_from_history_failure()
        
        print("=" * 60)
        print("✅ All tests passed! GitHub Actions history fallback is working correctly.")
        return True
        
    except Exception as e:
        print("=" * 60)
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)