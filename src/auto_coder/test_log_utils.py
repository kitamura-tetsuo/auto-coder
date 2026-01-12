"""
Utility functions for parsing test log output and extracting test failure information.

This module provides functionality to parse test output from various testing frameworks
(pytest, Playwright, Vitest) and extract information about failed tests.
"""

import os
import re
from typing import List, Optional

from .test_result import TestResult
from .logger_config import get_logger

logger = get_logger(__name__)


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


def extract_all_failed_tests(stdout: str, stderr: str = "") -> List[str]:
    """Extract and return a list of failed test files from test output.

    Args:
        stdout: The stdout output from the test run
        stderr: The stderr output from the test run

    Returns:
        List of unique paths for failed test files.
    """
    # Analyze stderr first, then stdout, if neither found, analyze combined output
    ordered_outputs = [stderr, stdout, f"{stdout}\\n{stderr}"]
    
    # Use a set to maintain uniqueness while collecting
    candidates_set = set()
    
    for output in ordered_outputs:
        failed_library = _detect_failed_test_library(output)

        if failed_library == "pytest":
            found = _collect_pytest_candidates(output)
            candidates_set.update(found)
        elif failed_library == "playwright":
            found = _collect_playwright_candidates(output)
            candidates_set.update(found)
        elif failed_library == "vitest":
            found = _collect_vitest_candidates(output)
            candidates_set.update(found)
            
        if candidates_set:
            break

    final_list = []
    # Sort candidates to be deterministic
    for path in sorted(list(candidates_set)):
        if os.path.exists(path):
            final_list.append(path)
            
    # If no existing files found, return logical candidates
    if not final_list and candidates_set:
        return sorted(list(candidates_set))
        
    return final_list


def extract_important_errors(test_result: TestResult) -> str:
    """Extract important error information from test output.

    Preserves multi-framework detection (pytest, Playwright, Vitest),
    Unicode markers (×, ›), and ANSI-friendly parsing.
    """
    if test_result.success:
        return ""

    errors = test_result.errors or ""
    output = test_result.output or ""

    # Combine stderr and stdout
    full_output = f"{errors}\n{output}".strip()
    logger.debug(
        "extract_important_errors: len(output)=%d len(errors)=%d framework=%s",
        len(output),
        len(errors),
        test_result.framework_type or "unknown",
    )

    # Prepend stability warning if detected
    prefix = ""
    if test_result.stability_issue:
        prefix = f"Test stability issue detected: {test_result.test_file or 'unknown'} failed in full suite but passed in isolation.\n\n"

    if not full_output:
        return prefix + "Tests failed but no error output available"

    lines = full_output.split("\n")

    # 0) If test detail lines with expected/received (Playwright/Jest) are included, prioritize extracting around the header
    if ("Expected substring:" in full_output) or ("Received string:" in full_output) or ("expect(received)" in full_output):
        try:
            import re

            # Find candidate header by scanning backwards
            # Playwright header: allow cases without leading "Error:" and allow leading spaces or the × mark
            hdr_pat = re.compile(r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\).*\.spec\.ts:.*")
            idx_expect = None
            for i, ln in enumerate(lines):
                if ("Expected substring:" in ln) or ("Received string:" in ln) or ("expect(received)" in ln):
                    idx_expect = i
                    break
            if idx_expect is not None:
                start = 0
                for j in range(idx_expect, -1, -1):
                    if hdr_pat.search(lines[j]):
                        start = j
                        break
                end = min(len(lines), idx_expect + 60)
                block_str = "\n".join(lines[start:end])
                if block_str:
                    return prefix + block_str
        except Exception:
            pass

    # 1) Prefer Playwright-typical error pattern extraction
    try:
        import re

        # Failure header: "Error:   1) [suite] › e2e/... .spec.ts:line:col › ..."
        header_indices = []
        # Playwright header: allow both with/without leading "Error:" and also leading whitespace or the × mark
        header_regex = re.compile(
            r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+\[[^\]]+\]\s+\u203a\s+.*\.spec\.ts:\d+:\d+\s+\u203a\s+.*|" r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+.*\.spec\.ts:.*",
            re.UNICODE,
        )
        for idx, ln in enumerate(lines):
            if header_regex.search(ln):
                header_indices.append(idx)
        # Typical expected/received patterns
        expect_regex = re.compile(r"expect\(received\).*|Expected substring:|Received string:")

        blocks = []
        for start_idx in header_indices:
            end_idx = min(len(lines), start_idx + 120)  # wider context up to 120 lines
            # Stop at the next error header (do not stop at empty lines)
            for j in range(start_idx + 1, min(len(lines), start_idx + 300)):
                if j >= len(lines):
                    break
                s = lines[j]
                if header_regex.search(s):
                    end_idx = j
                    break
            block = "\n".join(lines[start_idx:end_idx])
            # Check if it includes expectation/received lines or the corresponding expect line
            if any(expect_regex.search(b) for b in lines[start_idx:end_idx]) or any(".spec.ts" in b for b in lines[start_idx:end_idx]):
                blocks.append(block)
        if blocks:
            result = "\n\n".join(blocks)
            # Append expectation/received lines if not included
            if "Expected substring:" not in result or "Received string:" not in result:
                extra_lines = []
                for i, ln in enumerate(lines):
                    if "Expected substring:" in ln or "Received string:" in ln or "expect(received)" in ln:
                        start = max(0, i - 2)
                        end = min(len(lines), i + 4)
                        extra_lines.extend(lines[start:end])
                if extra_lines:
                    result = result + "\n\n--- Expectation Details ---\n" + "\n".join(extra_lines)
            return prefix + result
    except Exception:
        pass

    # 2) Keyword-based fallback extraction
    important_lines = []
    # Keywords that indicate important error information
    error_keywords = [
        # error detection
        "error:",
        "Error:",
        "ERROR:",
        "error",
        # failed detection
        "failed:",
        "Failed:",
        "FAILED:",
        "failed",
        # exceptions and traces
        "exception:",
        "Exception:",
        "EXCEPTION:",
        "traceback:",
        "Traceback:",
        "TRACEBACK:",
        # assertions and common python errors
        "assertion",
        "Assertion",
        "ASSERTION",
        "syntax error",
        "SyntaxError",
        "import error",
        "ImportError",
        "module not found",
        "ModuleNotFoundError",
        "test failed",
        "Test failed",
        "TEST FAILED",
        # e2e / Playwright related
        "e2e/",
        ".spec.ts",
        "playwright",
    ]

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(keyword.lower() in line_lower for keyword in error_keywords):
            # Extract a slightly broader context
            start = max(0, i - 5)
            end = min(len(lines), i + 8)
            context_lines = lines[start:end]
            important_lines.extend(context_lines)

    # Remove duplicates while preserving order
    seen = set()
    unique_lines = []
    for line in important_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)

    # Limit output length
    result = "\n".join(unique_lines)

    return prefix + (result if result else "Tests failed but no specific error information found")
