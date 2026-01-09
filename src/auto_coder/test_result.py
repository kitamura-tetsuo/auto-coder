"""Structured test result type for local and CI test runs.

Provides a type-safe alternative to loosely-typed Dict[str, Any] payloads
used across the automation engine. This improves maintainability and enables
clearer, LLM-friendly code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TestResult:
    """Container for a single test run result.

    Attributes:
        success: True when the test command exited successfully.
        output: Combined standard output from the test command.
        errors: Combined standard error from the test command.
        return_code: Process return code from the test command.
        command: The exact command used to run tests (for prompts/telemetry).
        test_file: Optional targeted test file when only a subset was executed.
        stability_issue: When True, indicates isolation/dependency problems
            (e.g., fails in full suite but passes in isolation).
        extraction_context: Arbitrary metadata attached by upstream collectors to
            aid error extraction/debugging.
        framework_type: Optional hint about the test framework (e.g., "pytest",
            "playwright", "vitest") to help error extraction heuristics.
    """

    success: bool
    output: str
    errors: str
    return_code: int
    command: str = ""
    test_file: Optional[str] = None
    stability_issue: bool = False
    # Error extraction metadata
    extraction_context: Dict[str, Any] = field(default_factory=dict)
    framework_type: Optional[str] = None
    # Raw JSON artifact data (e.g. from Playwright JSON report)
    json_artifact: Optional[Any] = None
