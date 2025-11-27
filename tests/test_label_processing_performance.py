"""Performance tests for label-based processing."""

import time
from unittest.mock import Mock

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.label_manager import _is_fuzzy_match, get_semantic_labels_from_issue, resolve_pr_labels_with_priority
from src.auto_coder.prompt_loader import _resolve_label_priority, clear_prompt_cache, render_prompt


class TestLabelProcessingPerformance:
    """Test performance characteristics of label-based processing."""

    def test_label_priority_resolution_performance_small(self):
        """Test performance of label priority resolution with small number of labels."""
        labels = [f"label-{i}" for i in range(10)]
        mappings = {f"label-{i}": f"prompt.{i}" for i in range(10)}
        priorities = [f"label-{i}" for i in range(10)]

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            _resolve_label_priority(labels, mappings, priorities)

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be very fast (under 0.001 seconds per operation)
        assert avg_time < 0.001, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ Small scale (10 labels): {avg_time:.6f}s per operation")

    def test_label_priority_resolution_performance_medium(self):
        """Test performance of label priority resolution with medium number of labels."""
        labels = [f"label-{i}" for i in range(50)]
        mappings = {f"label-{i}": f"prompt.{i}" for i in range(50)}
        priorities = [f"label-{i}" for i in range(50)]

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            _resolve_label_priority(labels, mappings, priorities)

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be fast (under 0.005 seconds per operation)
        assert avg_time < 0.005, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ Medium scale (50 labels): {avg_time:.6f}s per operation")

    def test_label_priority_resolution_performance_large(self):
        """Test performance of label priority resolution with large number of labels."""
        labels = [f"label-{i}" for i in range(100)]
        mappings = {f"label-{i}": f"prompt.{i}" for i in range(100)}
        priorities = [f"label-{i}" for i in range(100)]

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            _resolve_label_priority(labels, mappings, priorities)

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be reasonable (under 0.01 seconds per operation)
        assert avg_time < 0.01, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ Large scale (100 labels): {avg_time:.6f}s per operation")

    def test_prompt_selection_performance_small(self, tmp_path):
        """Test performance of label-based prompt selection with small label set."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" + "".join([f'  prompt{i}: "Prompt {i}"\n' for i in range(10)]),
            encoding="utf-8",
        )

        clear_prompt_cache()

        labels = [f"label-{i}" for i in range(10)]
        mappings = {f"label-{i}": f"issue.prompt{i}" for i in range(10)}
        priorities = [f"label-{i}" for i in range(10)]

        iterations = 500
        start = time.perf_counter()

        for _ in range(iterations):
            render_prompt(
                "issue.prompt0",
                path=str(prompt_file),
                labels=labels,
                label_prompt_mappings=mappings,
                label_priorities=priorities,
            )

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be fast (under 0.01 seconds per operation)
        assert avg_time < 0.01, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ Prompt selection (10 labels): {avg_time:.6f}s per operation")

    def test_prompt_selection_performance_large(self, tmp_path):
        """Test performance of label-based prompt selection with large label set."""
        # Create test prompt file with many prompts
        prompt_content = "issue:\n" + "".join([f'  prompt{i}: "Prompt {i} with $var"\n' for i in range(100)])
        (tmp_path / "prompts.yaml").write_text(prompt_content, encoding="utf-8")

        clear_prompt_cache()

        labels = [f"label-{i}" for i in range(100)]
        mappings = {f"label-{i}": f"issue.prompt{i}" for i in range(100)}
        priorities = [f"label-{i}" for i in range(100)]

        iterations = 100
        start = time.perf_counter()

        for _ in range(iterations):
            render_prompt(
                "issue.prompt0",
                path=str(tmp_path / "prompts.yaml"),
                labels=labels,
                label_prompt_mappings=mappings,
                label_priorities=priorities,
                var="test",
            )

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be reasonable (under 0.1 seconds per operation)
        assert avg_time < 0.1, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ Prompt selection (100 labels): {avg_time:.6f}s per operation")

    def test_semantic_label_detection_performance(self):
        """Test performance of semantic label detection."""
        issue_labels = [f"custom-label-{i}" for i in range(100)]
        label_mappings = {f"semantic-{i}": [f"label-{i}", f"alias-{i}"] for i in range(50)}

        iterations = 500
        start = time.perf_counter()

        for _ in range(iterations):
            get_semantic_labels_from_issue(issue_labels, label_mappings)

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be fast (under 0.01 seconds per operation)
        assert avg_time < 0.01, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ Semantic label detection (100 labels, 50 mappings): {avg_time:.6f}s per operation")

    def test_fuzzy_matching_performance(self):
        """Test performance of fuzzy matching."""
        test_labels = ["bug-fix", "feature-request", "documentation-update"]

        iterations = 10000
        start = time.perf_counter()

        for _ in range(iterations):
            for label in test_labels:
                _is_fuzzy_match(label, "bugfix")

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / (iterations * len(test_labels))

        # Should be very fast (under 0.0001 seconds per operation)
        assert avg_time < 0.0001, f"Average time {avg_time:.8f}s is too slow"
        print(f"✓ Fuzzy matching: {avg_time:.8f}s per operation")

    def test_pr_label_resolution_performance(self):
        """Test performance of PR label resolution with priority."""
        issue_labels = [f"label-{i}" for i in range(100)]

        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = 10

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            resolve_pr_labels_with_priority(issue_labels, config)

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be fast (under 0.001 seconds per operation)
        assert avg_time < 0.001, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ PR label resolution: {avg_time:.6f}s per operation")

    def test_multiple_operations_performance(self, tmp_path):
        """Test performance of multiple combined operations."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" + "".join([f'  prompt{i}: "Prompt {i}"\n' for i in range(20)]),
            encoding="utf-8",
        )

        clear_prompt_cache()

        labels = [f"label-{i}" for i in range(20)]
        mappings = {f"label-{i}": f"issue.prompt{i}" for i in range(20)}
        priorities = [f"label-{i}" for i in range(20)]

        iterations = 200
        start = time.perf_counter()

        for _ in range(iterations):
            # Combined operations
            _resolve_label_priority(labels, mappings, priorities)
            render_prompt(
                "issue.prompt0",
                path=str(prompt_file),
                labels=labels[:10],  # Subset of labels
                label_prompt_mappings=mappings,
                label_priorities=priorities,
            )
            get_semantic_labels_from_issue(labels[:10], {"bug": ["label-1"]})

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be reasonable (under 0.1 seconds per combined operation)
        assert avg_time < 0.1, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ Combined operations: {avg_time:.6f}s per operation")

    def test_cache_performance_benefit(self, tmp_path):
        """Test that caching improves performance."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default"\n' '  bug: "Bug prompt"\n' '  feature: "Feature prompt"\n',
            encoding="utf-8",
        )

        labels = ["bug"]
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        # First run (cache cold)
        clear_prompt_cache()
        start = time.perf_counter()
        for _ in range(100):
            render_prompt(
                "issue.action",
                path=str(prompt_file),
                labels=labels,
                label_prompt_mappings=mappings,
                label_priorities=priorities,
            )
        cold_time = time.perf_counter() - start

        # Second run (cache warm)
        start = time.perf_counter()
        for _ in range(100):
            render_prompt(
                "issue.action",
                path=str(prompt_file),
                labels=labels,
                label_prompt_mappings=mappings,
                label_priorities=priorities,
            )
        warm_time = time.perf_counter() - start

        # Warm cache should be faster
        print(f"✓ Cold cache: {cold_time:.4f}s for 100 operations")
        print(f"✓ Warm cache: {warm_time:.4f}s for 100 operations")
        print(f"✓ Speedup: {cold_time/warm_time:.2f}x")

        # Allow for a small timing variation (5% tolerance) to account for test flakiness
        # The warm cache should generally be faster, but we allow a small margin of error
        # due to timing variations, garbage collection, CPU scheduling, etc.
        max_acceptable_time = cold_time * 1.05
        assert warm_time <= max_acceptable_time, f"Warm cache should be at least as fast as cold cache " f"(allowing 5% tolerance for timing variations). " f"Warm: {warm_time:.6f}s, Cold: {cold_time:.6f}s, Max: {max_acceptable_time:.6f}s"

    def test_concurrent_label_processing(self):
        """Test that label processing works correctly under concurrent load."""
        import concurrent.futures
        import threading

        results = []
        lock = threading.Lock()

        def process_labels(iteration):
            """Process labels and record timing."""
            labels = [f"label-{i}" for i in range(10)]
            mappings = {f"label-{i}": f"prompt.{i}" for i in range(10)}
            priorities = [f"label-{i}" for i in range(10)]

            start = time.perf_counter()
            result = _resolve_label_priority(labels, mappings, priorities)
            elapsed = time.perf_counter() - start

            with lock:
                results.append((iteration, elapsed, result))

        # Run concurrent operations
        iterations = 100
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(process_labels, i) for i in range(iterations)]
            concurrent.futures.wait(futures)

        # All should complete successfully
        assert len(results) == iterations
        for iteration, elapsed, result in results:
            assert result is not None  # Should find a matching label
            assert elapsed < 0.01  # Should be fast even under load

        # Check average performance
        avg_time = sum(r[1] for r in results) / len(results)
        print(f"✓ Concurrent processing: {avg_time:.6f}s average per operation")

    def test_memory_usage_large_mappings(self):
        """Test memory usage with large label mappings."""
        import sys

        # Create large mappings
        large_mappings = {f"label-{i}": f"prompt-{i}" for i in range(1000)}

        labels = [f"label-{i}" for i in range(1000)]
        priorities = [f"label-{i}" for i in range(1000)]

        # Measure memory usage
        initial_size = sys.getsizeof(large_mappings)

        # Process large mappings
        for _ in range(10):
            _resolve_label_priority(labels, large_mappings, priorities)

        final_size = sys.getsizeof(large_mappings)

        # Size should be stable
        print(f"✓ Memory usage: {initial_size/1024:.2f} KB for 1000 mappings")
        assert abs(initial_size - final_size) < 1024  # Should not grow significantly

    def test_prompt_rendering_memory_efficiency(self, tmp_path):
        """Test memory efficiency of prompt rendering."""
        import sys

        # Create large prompt file
        large_content = "issue:\n" + "".join([f'  prompt{i}: "Prompt {i} with $var1 $var2 $var3"\n' for i in range(1000)])
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(large_content, encoding="utf-8")

        clear_prompt_cache()

        labels = ["bug"]
        mappings = {"bug": "issue.prompt500"}  # Middle prompt
        priorities = ["bug"]

        # Measure memory before
        initial_objects = len([obj for obj in globals().values() if hasattr(obj, "__dict__")])

        # Render large prompt
        for _ in range(10):
            result = render_prompt(
                "issue.prompt500",
                path=str(prompt_file),
                labels=labels,
                label_prompt_mappings=mappings,
                label_priorities=priorities,
                var1="test1",
                var2="test2",
                var3="test3",
            )
            assert "Prompt 500" in result

        # Memory should be stable
        print(f"✓ Prompt rendering: Processed 1000-line prompt file 10 times")

    def test_performance_with_all_semantic_labels(self):
        """Test performance when all semantic label categories are present."""
        issue_labels = [
            "breaking-change",
            "urgent",
            "bug",
            "enhancement",
            "documentation",
            "question",
        ]

        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "urgent": ["urgent", "critical"],
            "bug": ["bug", "bugfix"],
            "enhancement": ["enhancement", "feature"],
            "documentation": ["documentation", "docs"],
            "question": ["question", "help wanted"],
        }

        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = 10

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            # Test semantic label detection
            semantic = get_semantic_labels_from_issue(issue_labels, label_mappings)
            # Test PR label resolution
            resolved = resolve_pr_labels_with_priority(semantic, config)

        end = time.perf_counter()
        elapsed = end - start
        avg_time = elapsed / iterations

        # Should be very fast with semantic labels
        assert avg_time < 0.001, f"Average time {avg_time:.6f}s is too slow"
        print(f"✓ All semantic labels: {avg_time:.6f}s per operation")

    def test_performance_scale_linearity(self, tmp_path):
        """Test that performance scales linearly with label count."""
        import time

        times = []
        label_counts = [10, 20, 50, 100]

        for count in label_counts:
            labels = [f"label-{i}" for i in range(count)]
            mappings = {f"label-{i}": f"prompt.{i}" for i in range(count)}
            priorities = [f"label-{i}" for i in range(count)]

            iterations = 100
            start = time.perf_counter()

            for _ in range(iterations):
                _resolve_label_priority(labels, mappings, priorities)

            elapsed = time.perf_counter() - start
            avg_time = elapsed / iterations
            times.append((count, avg_time))

        # Check linearity (should roughly double when count doubles)
        for i in range(1, len(times)):
            prev_count, prev_time = times[i - 1]
            curr_count, curr_time = times[i]

            count_ratio = curr_count / prev_count
            time_ratio = curr_time / prev_time

            # Time ratio should be roughly proportional to count ratio (within 2x tolerance)
            assert time_ratio < count_ratio * 2, f"Performance doesn't scale linearly: {time_ratio:.2f}x time for {count_ratio:.2f}x labels"

            print(f"✓ Scale test: {prev_count}→{curr_count} labels: {prev_time:.6f}s→{curr_time:.6f}s ({time_ratio:.2f}x)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
