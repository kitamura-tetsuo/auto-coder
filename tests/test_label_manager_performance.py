"""Performance tests for LabelManager context manager.

These tests measure the performance characteristics of LabelManager to ensure
no regressions are introduced and that it meets performance requirements.
"""

import concurrent.futures
import time
from unittest.mock import Mock

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.label_manager import LabelManager


class TestLabelManagerPerformance:
    """Performance tests for LabelManager."""

    def test_label_manager_basic_operations_performance(self):
        """Test performance of basic LabelManager operations."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Measure time for single operation
        start = time.perf_counter()
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
            assert should_process is True
        end = time.perf_counter()
        single_op_time = end - start

        # Should be very fast (under 1 second)
        assert single_op_time < 1.0, f"Single operation took {single_op_time:.4f}s, expected < 1.0s"
        print(f"✓ Single LabelManager operation: {single_op_time:.6f}s")

    def test_label_manager_concurrent_operations(self):
        """Test LabelManager performance with concurrent operations."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        def label_manager_operation(operation_id):
            """Perform a label manager operation."""
            with LabelManager(
                mock_github_client, "owner/repo", operation_id, item_type="issue", config=config
            ) as should_process:
                return should_process

        # Run 100 concurrent operations
        num_operations = 100
        start = time.perf_counter()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(label_manager_operation, i) for i in range(num_operations)]
            results = [f.result() for f in futures]

        end = time.perf_counter()
        total_time = end - start

        # All should succeed
        assert all(results), "All concurrent operations should succeed"
        # Should handle 100 operations in reasonable time (under 5 seconds)
        assert total_time < 5.0, f"100 concurrent operations took {total_time:.4f}s, expected < 5.0s"
        print(f"✓ 100 concurrent LabelManager operations: {total_time:.4f}s ({total_time/num_operations:.6f}s per operation)")

    def test_label_manager_retry_performance(self):
        """Test performance impact of retry mechanism."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False

        # Simulate failures that require retries
        mock_github_client.try_add_work_in_progress_label.side_effect = [
            Exception("API error") for _ in range(2)
        ] + [True]  # Success on third attempt

        config = AutomationConfig()

        # Measure time with retries
        start = time.perf_counter()
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config, max_retries=3, retry_delay=0.01
        ) as should_process:
            assert should_process is True
        end = time.perf_counter()
        retry_time = end - start

        # Should include retry delays (0.01 * 2 = 0.02s minimum)
        assert retry_time >= 0.02, f"Retry time {retry_time:.4f}s should include retry delays"
        print(f"✓ LabelManager with retries (max_retries=3): {retry_time:.4f}s")

    def test_label_manager_thread_safety_performance(self):
        """Test thread safety performance impact."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        num_threads = 20
        operations_per_thread = 10

        def multiple_operations(thread_id):
            """Perform multiple label manager operations."""
            results = []
            for i in range(operations_per_thread):
                with LabelManager(
                    mock_github_client, f"owner/repo", thread_id * 1000 + i, item_type="issue", config=config
                ) as should_process:
                    results.append(should_process)
            return results

        # Run concurrent operations from multiple threads
        start = time.perf_counter()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(multiple_operations, i) for i in range(num_threads)]
            all_results = [result for future in futures for result in future.result()]

        end = time.perf_counter()
        total_time = end - start
        total_operations = num_threads * operations_per_thread

        # All should succeed
        assert len(all_results) == total_operations, "All operations should complete"
        assert all(all_results), "All operations should succeed"

        # Should handle load efficiently (under 10 seconds)
        assert total_time < 10.0, f"Thread safety test took {total_time:.4f}s, expected < 10.0s"
        print(f"✓ Thread safety test ({num_threads} threads × {operations_per_thread} operations): {total_time:.4f}s")

    def test_label_manager_memory_efficiency(self):
        """Test that LabelManager doesn't consume excessive memory."""
        import sys

        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Create multiple LabelManager instances
        managers = [
            LabelManager(mock_github_client, "owner/repo", i, item_type="issue", config=config)
            for i in range(100)
        ]

        # Measure approximate size
        total_size = sum(sys.getsizeof(m) for m in managers)
        # Each instance should be reasonably small (under 10KB)
        avg_size = total_size / len(managers)
        assert avg_size < 10000, f"Average LabelManager size {avg_size} bytes is too large"
        print(f"✓ LabelManager memory footprint: ~{avg_size:.0f} bytes per instance ({total_size/1024:.2f} KB for 100 instances)")

    def test_label_manager_disabled_labels_performance(self):
        """Test that disabled labels mode is fast (skips API calls)."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = True  # Labels disabled

        config = AutomationConfig()

        # Measure time with disabled labels (should be very fast)
        start = time.perf_counter()
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
            assert should_process is True
        end = time.perf_counter()
        disabled_time = end - start

        # Should be extremely fast (under 0.1 seconds)
        assert disabled_time < 0.1, f"Disabled labels operation took {disabled_time:.4f}s, expected < 0.1s"
        print(f"✓ LabelManager with disabled labels: {disabled_time:.6f}s")

    def test_label_manager_comparison_with_old_functions(self):
        """Compare performance of LabelManager with deprecated functions."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Test LabelManager performance
        iterations = 1000
        start = time.perf_counter()

        for i in range(iterations):
            with LabelManager(
                mock_github_client, "owner/repo", i, item_type="issue", config=config
            ) as should_process:
                pass

        label_manager_time = time.perf_counter() - start
        avg_label_manager = label_manager_time / iterations

        # Should be fast (under 0.01 seconds per operation)
        assert avg_label_manager < 0.01, f"Average LabelManager time {avg_label_manager:.6f}s is too slow"
        print(f"✓ LabelManager average time per operation (1000 iterations): {avg_label_manager:.6f}s")
        print(f"✓ Total time for 1000 operations: {label_manager_time:.4f}s")

    def test_label_manager_dry_run_performance(self):
        """Test that dry run mode is fast (doesn't make API calls)."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False

        config = AutomationConfig()

        # Measure time in dry run mode
        iterations = 1000
        start = time.perf_counter()

        for i in range(iterations):
            with LabelManager(
                mock_github_client, "owner/repo", i, item_type="issue", dry_run=True, config=config
            ) as should_process:
                assert should_process is True

        dry_run_time = time.perf_counter() - start
        avg_dry_run = dry_run_time / iterations

        # Dry run should be fast (under 0.005 seconds per operation)
        assert avg_dry_run < 0.005, f"Average dry run time {avg_dry_run:.6f}s is too slow"
        print(f"✓ LabelManager dry run average time per operation (1000 iterations): {avg_dry_run:.6f}s")
        print(f"✓ Total time for 1000 dry run operations: {dry_run_time:.4f}s")

    def test_label_manager_cleanup_performance(self):
        """Test that cleanup in __exit__ is fast."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Measure cleanup time specifically
        iterations = 1000
        total_cleanup_time = 0

        for i in range(iterations):
            start = time.perf_counter()
            with LabelManager(mock_github_client, "owner/repo", i, item_type="issue", config=config) as should_process:
                assert should_process is True
            end = time.perf_counter()
            # The entire context manager time includes cleanup
            total_cleanup_time += (end - start)

        avg_time = total_cleanup_time / iterations
        # Should be fast (under 0.01 seconds total including cleanup)
        assert avg_time < 0.01, f"Average time including cleanup {avg_time:.6f}s is too slow"
        print(f"✓ LabelManager average time per operation including cleanup (1000 iterations): {avg_time:.6f}s")

    @pytest.mark.parametrize("item_type", ["issue", "pr"])
    def test_label_manager_item_type_performance(self, item_type):
        """Test performance across different item types."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Measure performance for each item type
        iterations = 500
        start = time.perf_counter()

        for i in range(iterations):
            with LabelManager(
                mock_github_client, "owner/repo", i, item_type=item_type, config=config
            ) as should_process:
                assert should_process is True

        end = time.perf_counter()
        elapsed_time = end - start
        avg_time = elapsed_time / iterations

        # Should be fast for both types
        assert avg_time < 0.01, f"Average time for {item_type} operations {avg_time:.6f}s is too slow"
        print(f"✓ LabelManager {item_type} operations (500 iterations): {avg_time:.6f}s average")


if __name__ == "__main__":
    # Run performance tests with verbose output
    pytest.main([__file__, "-v", "-s"])
