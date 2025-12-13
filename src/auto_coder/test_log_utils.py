"""
Utility functions for parsing test log output and extracting test failure information.

This module provides functionality to parse test output from various testing frameworks
(pytest, Playwright, Vitest) and extract information about failed tests.
"""

import os
import re
from typing import List, Optional


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    return ansi_escape.sub("", text or "")


def _detect_failed_test_library(text: str) -> Optional[str]:
    """Determine which test library failed.

    Args:
        text: The test output text to analyze

    Returns:
        "pytest" | "playwright" | "vitest" | None
    """
    text = _strip_ansi(text)
    if not text:
        return None

    # pytest failure patterns
    if re.search(r"^FAILED\s+[^\s:]+\.py", text, re.MULTILINE):
        return "pytest"
    if re.search(r"=+ FAILURES =+", text, re.MULTILINE):
        return "pytest"
    if re.search(r"=+ \d+ failed", text, re.MULTILINE):
        return "pytest"
    # pytest traceback lines (tests/ directory .py files)
    if re.search(r"(?:^|\s)(?:tests?/)[^:\s]+\.py:\d+", text, re.MULTILINE):
        return "pytest"

    # Playwright failure patterns
    if re.search(r"^\s*[✘×xX]\s+\d+\s+\[[^\]]+\]\s+›", text, re.MULTILINE):
        return "playwright"
    if re.search(r"^\s*\d+\)\s+\[[^\]]+\]\s+›\s+[^\s:]+\.spec\.ts", text, re.MULTILINE):
        return "playwright"
    if re.search(r"\d+ failed.*playwright", text, re.IGNORECASE):
        return "playwright"
    # Playwright list reporter summary patterns
    if re.search(r"^\s*\d+\s+(?:failed|flaky)", text, re.MULTILINE):
        return "playwright"

    # Vitest failure patterns
    # Patterns like "FAIL  |unit| src/tests/..."
    # Match even in the middle of lines to handle log output
    if re.search(r"FAIL\s+(?:\|[^|]+\|\s+)?[^\s>]+\.(?:spec|test)\.ts", text):
        return "vitest"
    if re.search(r"Test Files\s+\d+ failed", text, re.MULTILINE):
        return "vitest"

    return None


def _normalize_spec(path: str) -> str:
    """Normalize Playwright spec file path."""
    m_e2e = re.search(r"(?:^|/)(e2e/[A-Za-z0-9_./-]+\.spec\.ts)$", path)
    return m_e2e.group(1) if m_e2e else path


def _collect_pytest_candidates(text: str) -> List[str]:
    """Extract pytest failed test files.

    Args:
        text: The test output text to analyze

    Returns:
        List of candidate paths for failed pytest test files
    """
    text = _strip_ansi(text)
    if not text:
        return []

    found: List[str] = []

    # 1) Extract from pytest FAILED summary lines
    for pat in [
        r"^FAILED\s+([^\s:]+\.py)::",
        r"^FAILED\s+([^\s:]+\.py)\s*[-:]",
        r"^FAILED\s+([^\s:]+\.py)\b",
    ]:
        m = re.search(pat, text, re.MULTILINE)
        if m:
            found.append(m.group(1))
            break

    # 2) Extract .py files under tests/ from pytest traceback lines
    m = re.search(r"(^|\s)((?:tests?/|^tests?/)[^:\s]+\.py):\d+", text, re.MULTILINE)
    if m:
        py_path = m.group(2)
        if py_path not in found:
            found.append(py_path)

    return found


def _collect_playwright_candidates(text: str) -> List[str]:
    """Extract Playwright failed test files.

    Args:
        text: The test output text to analyze

    Returns:
        List of candidate paths for failed Playwright test files
    """
    text = _strip_ansi(text)
    if not text:
        return []

    found: List[str] = []
    lines = text.split("\n")

    # Regex to identify section headers like "  1 failed", "  2 flaky", etc.
    section_header_re = re.compile(r"^\s*\d+\s+(failed|flaky|passed|skipped|did not run)")

    # Regex to extract spec files from lines
    # Matches: [project] › path/to/file.spec.ts:7:5 › ...
    # Also matches lines that just contain the spec path
    spec_file_re = re.compile(r"([^\s:]+\.spec\.ts)")

    current_section = None
    failed_candidates: List[str] = []
    flaky_candidates: List[str] = []

    for ln in lines:
        # Check for section header
        m_header = section_header_re.search(ln)
        if m_header:
            current_section = m_header.group(1)
            continue

        # Extract spec files from the line
        if current_section in ["failed", "flaky"]:
            specs = spec_file_re.findall(ln)
            for spec in specs:
                norm = _normalize_spec(spec)

                if current_section == "failed":
                    if norm not in failed_candidates:
                        failed_candidates.append(norm)
                elif current_section == "flaky":
                    if norm not in flaky_candidates:
                        flaky_candidates.append(norm)

    # Prioritize failed candidates, then flaky
    # Filter out duplicates while preserving order
    all_candidates = []
    for c in failed_candidates:
        if c not in all_candidates:
            all_candidates.append(c)
    for c in flaky_candidates:
        if c not in all_candidates:
            all_candidates.append(c)

    # Fallback: if nothing found in explicit sections, use the original regexes
    if not all_candidates:
        fail_bullet_re = re.compile(r"^[^\S\r\n]*[✘×xX]\s+\d+\s+\[[^\]]+\]\s+›\s+([^\s:]+\.spec\.ts):\d+:\d+")
        fail_heading_re = re.compile(r"^[^\S\r\n]*\d+\)\s+\[[^\]]+\]\s+›\s+([^\s:]+\.spec\.ts):\d+:\d+")

        for ln in lines:
            m = fail_bullet_re.search(ln)
            if m:
                norm = _normalize_spec(m.group(1))
                if norm not in all_candidates:
                    all_candidates.append(norm)

        for ln in lines:
            m = fail_heading_re.search(ln)
            if m:
                norm = _normalize_spec(m.group(1))
                if norm not in all_candidates:
                    all_candidates.append(norm)

    # Final Fallback: Search for lines containing .spec.ts (broad search)
    # Only if absolutely nothing else was found
    if not all_candidates:
        for spec_path in re.findall(r"([^\s:]+\.spec\.ts)", text):
            norm = _normalize_spec(spec_path)
            if norm not in all_candidates:
                all_candidates.append(norm)

    return all_candidates


def _collect_vitest_candidates(text: str) -> List[str]:
    """Extract Vitest failed test files.

    Args:
        text: The test output text to analyze

    Returns:
        List of candidate paths for failed Vitest test files
    """
    text = _strip_ansi(text)
    if not text:
        return []

    found: List[str] = []

    # Extract .test.ts / .spec.ts from Vitest/Jest format FAIL lines
    # Match even in the middle of lines to handle log output
    vitest_fail_re = re.compile(
        r"FAIL\s+(?:\|[^|]+\|\s+)?([^\s>]+\.(?:spec|test)\.ts)(?=\s|>|$)",
    )
    for m in vitest_fail_re.finditer(text):
        path = m.group(1)
        if path not in found:
            found.append(path)

    return found


def extract_first_failed_test(stdout: str, stderr: str) -> Optional[str]:
    """Extract and return the "path of the first failed test file" from test output.

    Two-stage detection method:
    1. First, determine which test library failed
    2. Then, extract the failed test file using patterns specific to that test library

    Supported formats:
    - pytest: End summary "FAILED tests/test_x.py::test_y - ..." etc.
    - pytest: Traceback "tests/test_x.py:123: in test_y" etc.
    - Playwright: Any log "e2e/foo/bar.spec.ts:16:5" etc.
    - Vitest: "FAIL src/foo.test.ts" etc.

    Args:
        stdout: The stdout output from the test run
        stderr: The stderr output from the test run

    Returns:
        The found path. May return a candidate even if existence check fails (interpreted by caller).
        Returns None if no failed test could be detected.
    """
    # Analyze stderr first, then stdout, if neither found, analyze combined output as before
    ordered_outputs = [stderr, stdout, f"{stdout}\n{stderr}"]
    candidates: List[str] = []

    for output in ordered_outputs:
        # Step 1: Determine which test library failed
        failed_library = _detect_failed_test_library(output)

        if failed_library == "pytest":
            candidates = _collect_pytest_candidates(output)
        elif failed_library == "playwright":
            candidates = _collect_playwright_candidates(output)
        elif failed_library == "vitest":
            candidates = _collect_vitest_candidates(output)

        if candidates:
            break

    # Prefer to return existing files
    for path in candidates:
        if os.path.exists(path):
            return path

    # If candidates exist, return the first candidate even if it doesn't exist
    if candidates:
        return candidates[0]

    return None
