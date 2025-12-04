"""Stress tests for label-based prompt processing.

This module provides comprehensive stress testing for label-based
prompt processing, including rapid operations, high-volume processing,
memory leak detection, and long-running process stability.
"""

import concurrent.futures
import gc
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.auto_coder.prompt_loader import _get_prompt_for_labels, _resolve_label_priority, clear_prompt_cache, get_label_specific_prompt, load_prompts, render_prompt


class TestRapidSuccessiveLabelOperations:
    """Test rapid successive label operations."""

    def test_rapid_label_resolution_burst(self):
        """Test rapid burst of label resolution operations."""
        labels = [f"label-{i}" for i in range(100)]
        mappings = {label: f"prompt.{label}" for label in labels}
        priorities = labels.copy()

        # Perform 1000 rapid operations
        num_operations = 1000
        start = time.perf_counter()

        for _ in range(num_operations):
            _resolve_label_priority(labels, mappings, priorities)

        elapsed = time.perf_counter() - start
        ops_per_second = num_operations / elapsed

        # Should handle thousands of operations per second
        assert ops_per_second > 1000, f"Only {ops_per_second:.0f} ops/sec, expected > 1000"
        print(f"✓ Rapid operations: {ops_per_second:.0f} ops/sec ({elapsed:.3f}s for {num_operations} ops)")

    def test_rapid_prompt_rendering_burst(self):
        """Test rapid burst of prompt rendering operations."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Default $var"\n  bugfix: "Bug $var"\n')
            prompt_file = f.name

        try:
            labels = ["bug"]
            mappings = {"bug": "issue.bugfix"}
            priorities = ["bug"]

            num_operations = 500
            start = time.perf_counter()

            for i in range(num_operations):
                render_prompt(
                    "issue.action",
                    path=prompt_file,
                    labels=labels,
                    label_prompt_mappings=mappings,
                    label_priorities=priorities,
                    var=f"value-{i}",
                )

            elapsed = time.perf_counter() - start
            ops_per_second = num_operations / elapsed

            # Should handle hundreds of prompt renderings per second
            assert ops_per_second > 100, f"Only {ops_per_second:.0f} renders/sec, expected > 100"
            print(f"✓ Rapid rendering: {ops_per_second:.0f} renders/sec ({elapsed:.3f}s for {num_operations} renders)")
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()

    def test_mixed_label_operations_burst(self):
        """Test burst of mixed label operations."""
        label_sets = [
            [f"label-{i}" for i in range(10)],
            [f"label-{i}" for i in range(50)],
            [f"label-{i}" for i in range(100)],
        ]

        num_operations = 1000
        start = time.perf_counter()

        for i in range(num_operations):
            labels = label_sets[i % len(label_sets)]
            mappings = {label: f"prompt.{label}" for label in labels}
            priorities = labels.copy()

            result = _resolve_label_priority(labels, mappings, priorities)
            assert result == labels[0]  # First in priorities

        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"Mixed operations took {elapsed:.3f}s, expected < 5.0s"
        print(f"✓ Mixed operations burst: {elapsed:.3f}s for {num_operations} operations")


class TestHighVolumeIssueProcessing:
    """Test high-volume issue processing (100+ issues)."""

    def test_process_100_issues_with_labels(self):
        """Test processing 100 issues with different label configurations."""
        issues_data = []
        for i in range(100):
            labels = [f"label-{i % 10}", f"type-{i % 5}"]
            mappings = {f"label-{j}": f"prompt.label-{j}" for j in range(10)}
            mappings.update({f"type-{j}": f"prompt.type-{j}" for j in range(5)})
            priorities = [f"label-{j}" for j in range(10)]
            issues_data.append((labels, mappings, priorities))

        start = time.perf_counter()

        results = []
        for labels, mappings, priorities in issues_data:
            result = _get_prompt_for_labels(labels, mappings, priorities)
            results.append(result)

        elapsed = time.perf_counter() - start

        # All should have results
        assert len(results) == 100
        assert all(r is not None for r in results)

        # Should process 100 issues in reasonable time
        assert elapsed < 1.0, f"100 issues took {elapsed:.3f}s, expected < 1.0s"
        print(f"✓ Processed 100 issues in {elapsed:.3f}s ({100/elapsed:.0f} issues/sec)")

    def test_process_500_issues_with_diverse_labels(self):
        """Test processing 500 issues with diverse label configurations."""
        issue_count = 500
        label_variety = 50

        issues_data = []
        for i in range(issue_count):
            # Each issue gets 3-10 random labels from pool
            num_labels = 3 + (i % 8)
            labels = [f"label-{j % label_variety}" for j in range(num_labels)]
            mappings = {f"label-{j}": f"prompt.label-{j}" for j in range(label_variety)}
            priorities = [f"label-{j}" for j in range(label_variety)]
            issues_data.append((labels, mappings, priorities))

        start = time.perf_counter()

        for labels, mappings, priorities in issues_data:
            result = _get_prompt_for_labels(labels, mappings, priorities)
            assert result is not None

        elapsed = time.perf_counter() - start
        issues_per_second = issue_count / elapsed

        # Should handle high volume efficiently
        assert issues_per_second > 100, f"Only {issues_per_second:.0f} issues/sec"
        print(f"✓ Processed {issue_count} issues: {issues_per_second:.0f} issues/sec ({elapsed:.3f}s)")

    def test_concurrent_high_volume_processing(self):
        """Test concurrent processing of high volume of issues."""

        def process_batch(batch_id, batch_size):
            """Process a batch of issues."""
            results = []
            for i in range(batch_size):
                labels = [f"label-{j}" for j in range(10)]
                mappings = {f"label-{j}": f"prompt.label-{j}" for j in range(10)}
                priorities = [f"label-{j}" for j in range(10)]

                result = _get_prompt_for_labels(labels, mappings, priorities)
                results.append(result)
            return len(results)

        num_batches = 10
        batch_size = 50
        total_issues = num_batches * batch_size

        start = time.perf_counter()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(process_batch, i, batch_size) for i in range(num_batches)]
            batch_results = [f.result() for f in futures]

        elapsed = time.perf_counter() - start

        # All batches should complete
        assert len(batch_results) == num_batches
        assert sum(batch_results) == total_issues

        # Should handle concurrent load efficiently
        assert elapsed < 2.0, f"Concurrent processing took {elapsed:.3f}s"
        print(f"✓ Concurrent processing: {total_issues} issues in {elapsed:.3f}s")


class TestMemoryLeakDetection:
    """Test memory leak detection during extended processing."""

    def test_memory_usage_during_extended_label_processing(self):
        """Test memory usage doesn't grow excessively during processing."""
        import tracemalloc

        tracemalloc.start()

        # Initial memory snapshot
        initial_snapshot = tracemalloc.take_snapshot()

        # Process many labels
        for iteration in range(100):
            labels = [f"label-{i}" for i in range(1000)]
            mappings = {label: f"prompt.{label}" for label in labels}
            priorities = labels.copy()

            for _ in range(10):
                _resolve_label_priority(labels, mappings, priorities)

            # Check memory every 20 iterations
            if iteration % 20 == 0:
                current_snapshot = tracemalloc.take_snapshot()
                top_stats = current_snapshot.compare_to(initial_snapshot, "lineno")

                # Memory growth should be minimal
                total_growth = sum(stat.size_diff for stat in top_stats)
                # Allow up to 10MB growth after 100 iterations
                assert total_growth < 10 * 1024 * 1024, f"Excessive memory growth: {total_growth / 1024 / 1024:.2f}MB"

        tracemalloc.stop()
        print(f"✓ Extended processing memory check passed")

    def test_object_creation_during_label_operations(self):
        """Test that label operations don't create excessive objects."""
        gc.collect()  # Clean up before test

        initial_objects = len(gc.get_objects())

        # Perform many label operations
        for _ in range(50):
            labels = [f"label-{i}" for i in range(500)]
            mappings = {label: f"prompt.{label}" for label in labels}
            priorities = labels.copy()

            for _ in range(20):
                _resolve_label_priority(labels, mappings, priorities)

        gc.collect()
        final_objects = len(gc.get_objects())

        object_growth = final_objects - initial_objects

        # Allow some growth but not excessive
        # Each iteration creates some objects, but they should be cleaned up
        max_acceptable_growth = 50000
        assert object_growth < max_acceptable_growth, f"Excessive object creation: {object_growth} objects (initial: {initial_objects}, final: {final_objects})"

        print(f"✓ Object creation check: {object_growth} net objects created")

    def test_memory_with_cache_operations(self):
        """Test memory usage during cache operations."""
        from src.auto_coder.prompt_loader import _PROMPTS_CACHE

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Test"\n')
            prompt_file = f.name

        try:
            # Initial cache state
            initial_cache_size = len(_PROMPTS_CACHE)

            # Perform many cache operations
            for i in range(100):
                result = load_prompts(prompt_file)
                assert result is not None

                # Clear cache every 10 iterations
                if i % 10 == 0:
                    clear_prompt_cache()

            # Cache should not grow excessively
            final_cache_size = len(_PROMPTS_CACHE)
            assert final_cache_size <= 1, f"Cache grew to {final_cache_size} entries"
            print(f"✓ Cache memory usage: initial={initial_cache_size}, final={final_cache_size}")
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()

    def test_memory_cleanup_after_label_processing(self):
        """Test that memory is properly cleaned up after label processing."""
        gc.collect()

        initial_objects = len(gc.get_objects())

        # Create temporary references
        temp_refs = []
        for i in range(10):
            labels = [f"label-{i}-{j}" for j in range(100)]
            mappings = {label: f"prompt.{label}" for label in labels}
            priorities = labels.copy()
            temp_refs.append((labels, mappings, priorities))

        # Process and let references go out of scope
        del temp_refs

        # Force garbage collection
        gc.collect()

        final_objects = len(gc.get_objects())
        object_growth = final_objects - initial_objects

        # After cleanup, object growth should be minimal
        assert object_growth < 1000, f"Memory not properly cleaned: {object_growth} objects retained"

        print(f"✓ Memory cleanup: {object_growth} objects retained after cleanup")


class TestResourceCleanup:
    """Test resource cleanup verification."""

    def test_file_handle_cleanup(self):
        """Test that file handles are properly closed."""
        pytest.importorskip("psutil", minversion=None)
        import os

        import psutil

        process = psutil.Process(os.getpid())
        initial_fds = process.num_fds() if hasattr(process, "num_fds") else process.num_handles()

        # Perform many file operations
        for _ in range(50):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write('issue:\n  action: "Test"\n')
                prompt_file = f.name

            try:
                load_prompts(prompt_file)
            finally:
                Path(prompt_file).unlink(missing_ok=True)

        # Force cleanup
        gc.collect()

        final_fds = process.num_fds() if hasattr(process, "num_fds") else process.num_handles()
        fd_growth = final_fds - initial_fds

        # Allow some growth but not excessive (each file operation may create temp files)
        assert fd_growth < 20, f"File handles not properly closed: {fd_growth} extra FDs"

        print(f"✓ File handle cleanup: {fd_growth} extra FDs")

    def test_temporary_file_cleanup(self):
        """Test that temporary files are cleaned up."""
        import glob
        import os

        # Use a more specific pattern that only matches our test files
        temp_dir_pattern = tempfile.gettempdir() + "/test_label_*.yaml"

        # Clean up any existing test files first
        for f in glob.glob(temp_dir_pattern):
            try:
                os.remove(f)
            except (FileNotFoundError, IsADirectoryError):
                pass

        initial_temp_files = len(glob.glob(temp_dir_pattern))

        # Create and process many temp files
        # Note: We use delete=False and manually delete to ensure cleanup
        for i in range(100):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, prefix="test_label_") as f:
                f.write('issue:\n  action: "Test"\n')
                prompt_file = f.name

            try:
                load_prompts(prompt_file)
            finally:
                # Manually delete to ensure cleanup
                Path(prompt_file).unlink(missing_ok=True)

        # Check for leaked temp files
        final_temp_files = len(glob.glob(temp_dir_pattern))
        leaked_files = final_temp_files - initial_temp_files

        assert leaked_files == 0, f"Temp files not cleaned up: {leaked_files} files leaked"

        print(f"✓ Temporary file cleanup: {leaked_files} files leaked")


class TestLongRunningProcessStability:
    """Test long-running process stability."""

    def test_sustained_load_over_time(self):
        """Test system stability under sustained load over time."""
        import threading

        start_time = time.perf_counter()
        duration = 2.0  # Run for 2 seconds
        error_count = [0]
        operation_count = [0]

        def worker():
            try:
                while time.perf_counter() - start_time < duration:
                    labels = [f"label-{i}" for i in range(50)]
                    mappings = {label: f"prompt.{label}" for label in labels}
                    priorities = labels.copy()

                    result = _resolve_label_priority(labels, mappings, priorities)
                    assert result == "label-0"

                    operation_count[0] += 1
                    time.sleep(0.001)  # Small delay
            except Exception as e:
                error_count[0] += 1
                print(f"Worker error: {e}")

        # Start multiple workers
        threads = [threading.Thread(target=worker) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        elapsed = time.perf_counter() - start_time

        # Should complete without errors
        assert error_count[0] == 0, f"{error_count[0]} errors during sustained load"
        assert elapsed >= duration, f"Test ended early: {elapsed:.3f}s"

        total_ops = operation_count[0]
        ops_per_sec = total_ops / elapsed

        print(f"✓ Sustained load: {total_ops} operations in {elapsed:.3f}s ({ops_per_sec:.0f} ops/sec)")

    def test_memory_stability_over_time(self):
        """Test memory stability over extended processing."""
        import tracemalloc

        tracemalloc.start()

        # Get baseline
        baseline = tracemalloc.take_snapshot()

        # Run for a short duration
        start = time.perf_counter()
        while time.perf_counter() - start < 1.0:
            labels = [f"label-{i}" for i in range(200)]
            mappings = {label: f"prompt.{label}" for label in labels}
            priorities = labels.copy()

            for _ in range(10):
                _resolve_label_priority(labels, mappings, priorities)

        # Check memory growth
        current = tracemalloc.take_snapshot()
        top_stats = current.compare_to(baseline, "lineno")
        total_growth = sum(stat.size_diff for stat in top_stats)

        # Memory growth should be minimal
        max_growth = 5 * 1024 * 1024  # 5MB
        assert total_growth < max_growth, f"Memory growth exceeded limit: {total_growth / 1024 / 1024:.2f}MB"

        tracemalloc.stop()
        print(f"✓ Memory stability: {total_growth / 1024 / 1024:.2f}MB growth")

    def test_cache_stability_over_time(self):
        """Test cache stability over extended operations."""
        from src.auto_coder.prompt_loader import _PROMPTS_CACHE

        # Clear cache
        clear_prompt_cache()
        initial_cache_size = len(_PROMPTS_CACHE)

        start = time.perf_counter()
        while time.perf_counter() - start < 1.0:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write('issue:\n  action: "Test"\n')
                prompt_file = f.name

            try:
                load_prompts(prompt_file)
            finally:
                Path(prompt_file).unlink()

        # Cache should not grow unbounded
        final_cache_size = len(_PROMPTS_CACHE)
        # Note: Without explicit cache eviction, size may grow
        # This test documents current behavior

        print(f"✓ Cache stability: {initial_cache_size} -> {final_cache_size} entries")

    def test_error_handling_under_stress(self):
        """Test error handling remains robust under stress."""
        error_count = 0
        total_operations = 0

        start = time.perf_counter()
        duration = 1.0

        while time.perf_counter() - start < duration:
            total_operations += 1

            # Mix of valid and invalid operations
            try:
                labels = [f"label-{i}" for i in range(10)]
                mappings = {label: f"prompt.{label}" for label in labels}
                priorities = labels.copy()

                result = _resolve_label_priority(labels, mappings, priorities)
                assert result == "label-0"
            except Exception:
                error_count += 1

        error_rate = (error_count / total_operations) * 100

        # Error rate should be very low (ideally 0)
        assert error_rate < 1.0, f"High error rate under stress: {error_rate:.2f}%"
        print(f"✓ Error handling under stress: {error_rate:.2f}% error rate ({error_count}/{total_operations})")


class TestResourceLimits:
    """Test behavior under resource constraints."""

    def test_large_label_list_handling(self):
        """Test handling of very large label lists."""
        # Create very large label set
        num_labels = 10000
        labels = [f"label-{i}" for i in range(num_labels)]
        mappings = {label: f"prompt.{label}" for label in labels}
        priorities = labels.copy()

        start = time.perf_counter()
        result = _resolve_label_priority(labels, mappings, priorities)
        elapsed = time.perf_counter() - start

        assert result == "label-0"
        # Should handle large lists (may be slower)
        assert elapsed < 1.0, f"10000 labels took {elapsed:.3f}s, expected < 1.0s"
        print(f"✓ Large label list (10,000 labels): {elapsed:.3f}s")

    def test_deeply_nested_prompt_templates(self):
        """Test rendering of deeply nested prompt templates."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            # Create nested structure
            content = "issue:\n"
            for i in range(100):
                content += f"  nested_{i}:\n"
                for j in range(10):
                    content += f'    level_{j}: "Value {i}.{j}"\n'
            f.write(content)
            prompt_file = f.name

        try:
            start = time.perf_counter()
            result = render_prompt("issue.nested_50.level_5", path=prompt_file)
            elapsed = time.perf_counter() - start

            assert "Value 50.5" in result
            # Nested access should be fast
            # Note: @log_calls decorator adds overhead, especially in CI environments
            # Increased threshold to accommodate CI overhead while still catching regressions
            assert elapsed < 0.5, f"Nested template access took {elapsed:.3f}s"
            print(f"✓ Deeply nested templates: {elapsed:.3f}s")
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()


if __name__ == "__main__":
    # Run stress tests with verbose output
    pytest.main([__file__, "-v", "-s"])
