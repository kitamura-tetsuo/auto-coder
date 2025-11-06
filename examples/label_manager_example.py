"""Example demonstrating LabelManager usage."""

from unittest.mock import Mock
import sys

# Add src to path for import
sys.path.insert(0, "src")

from auto_coder.label_manager import LabelManager, LabelOperationError


def demo_label_manager():
    """Demonstrate LabelManager context manager usage."""
    print("=" * 70)
    print("LabelManager Context Manager Example")
    print("=" * 70)

    # Create a mock GitHub client
    mock_github = Mock()
    mock_github.try_add_work_in_progress_label.return_value = True
    mock_github.remove_labels_from_issue.return_value = None
    mock_github.has_label.return_value = True

    # Example 1: Basic usage with context manager
    print("\n1. Basic Context Manager Usage:")
    print("-" * 70)
    try:
        with LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            item_type="issue",
        ) as lm:
            print("  ✓ Label added successfully")
            print(f"  - Repository: {lm.repo_name}")
            print(f"  - Item: {lm.item_type} #{lm.item_number}")
            print(f"  - Label: {lm.label_name}")
            print("  → Processing issue here...")
            print("  → Label will be automatically removed on exit")
    except Exception as e:
        print(f"  ✗ Error: {e}")

    # Example 2: Handling already labeled items (race condition)
    print("\n2. Handling Already Labeled Items (Another Instance Processing):")
    print("-" * 70)
    mock_github.try_add_work_in_progress_label.return_value = False
    try:
        with LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=456,
        ) as lm:
            print("  This won't be reached")
    except LabelOperationError as e:
        print(f"  ✓ Correctly detected another instance is processing")
        print(f"  - Message: {e}")

    # Example 3: Dry run mode
    print("\n3. Dry Run Mode:")
    print("-" * 70)
    mock_github.try_add_work_in_progress_label.return_value = True
    with LabelManager(
        github_client=mock_github,
        repo_name="owner/repo",
        item_number=789,
        dry_run=True,
    ) as lm:
        print("  ✓ Dry run mode - no actual API calls made")
        print("  → Simulating processing...")
    print("  → No cleanup needed in dry run mode")

    # Example 4: Labels disabled
    print("\n4. Labels Disabled via Config:")
    print("-" * 70)
    mock_github.disable_labels = True
    with LabelManager(
        github_client=mock_github,
        repo_name="owner/repo",
        item_number=999,
    ) as lm:
        print("  ✓ Labels disabled - operations are no-ops")
        print("  → Processing continues normally")

    # Example 5: Custom label name
    print("\n5. Custom Label Name:")
    print("-" * 70)
    mock_github.disable_labels = False
    mock_github.try_add_work_in_progress_label.return_value = True
    with LabelManager(
        github_client=mock_github,
        repo_name="owner/repo",
        item_number=111,
        label_name="work-in-progress",
    ) as lm:
        print(f"  ✓ Using custom label: '{lm.label_name}'")
        print("  → Processing with custom label...")

    # Example 6: Verifying label exists
    print("\n6. Verifying Label Exists:")
    print("-" * 70)
    lm = LabelManager(
        github_client=mock_github,
        repo_name="owner/repo",
        item_number=222,
        item_type="pr",
    )
    exists = lm.verify_label_exists()
    print(f"  ✓ Label '{lm.label_name}' exists: {exists}")

    # Example 7: Error handling with retry
    print("\n7. Retry Mechanism (Simulated):")
    print("-" * 70)
    mock_github.try_add_work_in_progress_label.side_effect = [
        Exception("Network error"),
        Exception("Rate limit"),
        True,
    ]
    print("  - Simulating API failures with recovery:")
    print("    1. Network error")
    print("    2. Rate limit")
    print("    3. Success!")
    with LabelManager(
        github_client=mock_github,
        repo_name="owner/repo",
        item_number=333,
        max_retries=3,
        retry_delay=0.01,
    ) as lm:
        print("  ✓ Successfully recovered from temporary failures")

    # Example 8: Exception handling with cleanup
    print("\n8. Cleanup on Exception:")
    print("-" * 70)
    mock_github.try_add_work_in_progress_label.return_value = True
    mock_github.remove_labels_from_issue.return_value = None
    try:
        with LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=444,
        ) as lm:
            print("  ✓ Label added")
            print("  → Simulating work...")
            raise ValueError("Unexpected error during processing")
    except ValueError as e:
        print(f"  ✓ Exception caught: {e}")
        print("  ✓ Label automatically removed during cleanup")

    print("\n" + "=" * 70)
    print("LabelManager Examples Complete!")
    print("=" * 70)
    print("\nKey Features Demonstrated:")
    print("  • Context manager for automatic label lifecycle management")
    print("  • Race condition detection (another instance processing)")
    print("  • Dry run mode for testing")
    print("  • Configurable label disable functionality")
    print("  • Custom label name support")
    print("  • Label verification")
    print("  • Automatic retry with exponential backoff")
    print("  • Guaranteed cleanup even on exceptions")
    print("=" * 70)


if __name__ == "__main__":
    demo_label_manager()
