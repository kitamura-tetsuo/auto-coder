"""Benchmark tests using pytest-benchmark for label-based prompt processing.

This module demonstrates pytest-benchmark usage and provides baseline
benchmarking for label-based prompt processing operations.
"""

import pytest

from src.auto_coder.prompt_loader import _resolve_label_priority


class TestLabelBenchmark:
    """Benchmark tests using pytest-benchmark decorator."""

    @pytest.mark.benchmark
    def test_benchmark_label_priority_resolution_10_labels(self, benchmark):
        """Benchmark label priority resolution with 10 labels."""
        labels = ["bug", "feature", "enhancement", "urgent", "documentation", "testing", "refactor", "bugfix", "improvement", "minor"]
        mappings = {label: f"prompt.{label}" for label in labels}
        priorities = labels.copy()

        result = benchmark(_resolve_label_priority, labels, mappings, priorities)
        assert result == "bug"

    @pytest.mark.benchmark
    def test_benchmark_label_priority_resolution_100_labels(self, benchmark):
        """Benchmark label priority resolution with 100 labels."""
        labels = [f"label-{i}" for i in range(100)]
        mappings = {label: f"prompt.{label}" for label in labels}
        priorities = labels.copy()

        result = benchmark(_resolve_label_priority, labels, mappings, priorities)
        assert result == "label-0"

    @pytest.mark.benchmark
    def test_benchmark_label_priority_resolution_1000_labels(self, benchmark):
        """Benchmark label priority resolution with 1000 labels."""
        labels = [f"label-{i}" for i in range(1000)]
        mappings = {label: f"prompt.{label}" for label in labels}
        priorities = labels.copy()

        result = benchmark(_resolve_label_priority, labels, mappings, priorities)
        assert result == "label-0"


if __name__ == "__main__":
    # Run benchmark tests
    pytest.main([__file__, "--benchmark-only", "-v"])
