#!/usr/bin/env python3
"""Simple test to verify LabelManager implementation."""

import sys
from unittest.mock import Mock

# Add src to path
sys.path.insert(0, '/workspaces/auto-coder/src')

from auto_coder.label_manager import LabelManager
from auto_coder.automation_config import AutomationConfig

def test_label_manager_basic():
    """Test basic LabelManager functionality."""
    print("Testing LabelManager basic functionality...")

    # Setup mocks
    mock_github_client = Mock()
    mock_github_client.disable_labels = False
    mock_github_client.has_label.return_value = False
    mock_github_client.try_add_work_in_progress_label.return_value = True

    config = AutomationConfig()

    # Test successful label management
    with LabelManager(
        mock_github_client, "owner/repo", 123, item_type="issue", config=config
    ) as should_process:
        assert should_process is True, "Expected should_process to be True"
        assert mock_github_client.try_add_work_in_progress_label.called, "Expected try_add_work_in_progress_label to be called"

    assert mock_github_client.remove_labels_from_issue.called, "Expected remove_labels_from_issue to be called"
    print("✓ Basic functionality test passed")

def test_label_manager_skip_when_exists():
    """Test that LabelManager skips when label already exists."""
    print("Testing LabelManager skip when label exists...")

    # Setup mocks
    mock_github_client = Mock()
    mock_github_client.disable_labels = False
    mock_github_client.has_label.return_value = True  # Label already exists

    config = AutomationConfig()

    # Test skip when label exists
    with LabelManager(
        mock_github_client, "owner/repo", 123, item_type="issue", config=config
    ) as should_process:
        assert should_process is False, "Expected should_process to be False when label exists"
        assert not mock_github_client.try_add_work_in_progress_label.called, "Should not add label when it exists"

    assert not mock_github_client.remove_labels_from_issue.called, "Should not remove label we didn't add"
    print("✓ Skip when exists test passed")

def test_label_manager_cleanup_on_exception():
    """Test that LabelManager cleans up on exception."""
    print("Testing LabelManager cleanup on exception...")

    # Setup mocks
    mock_github_client = Mock()
    mock_github_client.disable_labels = False
    mock_github_client.has_label.return_value = False
    mock_github_client.try_add_work_in_progress_label.return_value = True

    config = AutomationConfig()

    # Test cleanup on exception
    try:
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config
        ) as should_process:
            assert should_process is True
            raise ValueError("Test exception")
    except ValueError:
        pass

    assert mock_github_client.remove_labels_from_issue.called, "Expected cleanup on exception"
    print("✓ Cleanup on exception test passed")

def test_label_manager_disabled_labels():
    """Test that LabelManager skips when labels are disabled."""
    print("Testing LabelManager with disabled labels...")

    # Setup mocks
    mock_github_client = Mock()
    mock_github_client.disable_labels = True

    config = AutomationConfig()

    # Test with disabled labels
    with LabelManager(
        mock_github_client, "owner/repo", 123, item_type="issue", config=config
    ) as should_process:
        assert should_process is True, "Expected should_process to be True with disabled labels"
        assert not mock_github_client.try_add_work_in_progress_label.called, "Should not perform operations when disabled"

    assert not mock_github_client.remove_labels_from_issue.called, "Should not remove when disabled"
    print("✓ Disabled labels test passed")

def test_label_manager_dry_run():
    """Test that LabelManager works in dry_run mode."""
    print("Testing LabelManager dry_run mode...")

    # Setup mocks
    mock_github_client = Mock()
    mock_github_client.disable_labels = False
    mock_github_client.has_label.return_value = False

    config = AutomationConfig()

    # Test dry_run
    with LabelManager(
        mock_github_client, "owner/repo", 123, item_type="issue", dry_run=True, config=config
    ) as should_process:
        assert should_process is True
        assert not mock_github_client.try_add_work_in_progress_label.called, "Should not call API in dry_run"

    assert not mock_github_client.remove_labels_from_issue.called, "Should not remove in dry_run"
    print("✓ Dry run test passed")

if __name__ == "__main__":
    try:
        test_label_manager_basic()
        test_label_manager_skip_when_exists()
        test_label_manager_cleanup_on_exception()
        test_label_manager_disabled_labels()
        test_label_manager_dry_run()
        print("\n✅ All tests passed!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
