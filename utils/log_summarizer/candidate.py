from abc import ABC, abstractmethod
from typing import Dict, Type


class BaseSummarizer(ABC):
    @abstractmethod
    def summarize(self, log_content: str) -> str:
        """
        Summarize the log content.

        Args:
            log_content: The error log text.

        Returns:
            A summary string.
        """
        pass


class BaselineSummarizer(BaseSummarizer):
    def summarize(self, log_content: str) -> str:
        """
        Summarize the log content using a deterministic algorithm (e.g., regex).
        """
        # Baseline implementation: Extract lines containing "Error" or "Exception"
        lines = log_content.splitlines()
        error_lines = [line.strip() for line in lines if "error" in line.lower() or "exception" in line.lower() or "fail" in line.lower()]

        if not error_lines:
            return "No obvious error lines found in the log."

        # Return unique lines to avoid duplicates, preserving order
        seen = set()
        unique_errors = []
        for line in error_lines:
            if line not in seen:
                unique_errors.append(line)
                seen.add(line)

        # Limit to top 10 lines for brevity in baseline
        return "\n".join(unique_errors[:10])


# Registry for algorithms
ALGORITHM_REGISTRY: Dict[str, Type[BaseSummarizer]] = {
    "baseline": BaselineSummarizer,
}
