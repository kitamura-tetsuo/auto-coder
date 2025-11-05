import os
import textwrap

from src.auto_coder.utils import extract_first_failed_test


def test_extract_from_pytest_failed_summary_hyphen(monkeypatch):
    stdout = textwrap.dedent(
        """
        =========================== short test summary info ============================
        FAILED tests/test_foo.py::test_bar - AssertionError: boom
        1 failed in 0.12s
        """
    )
    stderr = ""

    expected_path = "tests/test_foo.py"

    monkeypatch.setattr(os.path, "exists", lambda p: p == expected_path)

    path = extract_first_failed_test(stdout, stderr)
    assert path == expected_path


def test_extract_from_pytest_traceback_line(monkeypatch):
    stdout = textwrap.dedent(
        """
        _________________________________ test_spam __________________________________

        tests/test_bar.py:12: in test_spam
            assert 1 == 2
        E   AssertionError: boom
        """
    )
    stderr = ""

    expected_path = "tests/test_bar.py"
    monkeypatch.setattr(os.path, "exists", lambda p: p == expected_path)

    path = extract_first_failed_test(stdout, stderr)
    assert path == expected_path


def test_extract_from_playwright_spec(monkeypatch):
    stdout = textwrap.dedent(
        """
        1) [suite] › e2e/basic/foo.spec.ts:16:5 › does something
           Error: expect(received).toBeTruthy()
        """
    )
    stderr = ""

    expected_path = "e2e/basic/foo.spec.ts"
    monkeypatch.setattr(os.path, "exists", lambda p: p == expected_path)

    path = extract_first_failed_test(stdout, stderr)
    assert path == expected_path


def test_extract_playwright_candidate_returned_even_if_not_exists():
    # Can return as candidate even with ANSI color or non-existent path
    stdout = "\x1b[31m  ✘    1 [basic] › e2e/basic/00-foo-bar.spec.ts:15:5 › title \x1b[39m\n" "\n  1) [basic] › e2e/basic/00-foo-bar.spec.ts:15:5 › title \n\n"
    stderr = ""

    path = extract_first_failed_test(stdout, stderr)
    assert path == "e2e/basic/00-foo-bar.spec.ts"


def test_extract_playwright_from_sample_full_output_excerpt():
    # Excerpt close to user-provided format (including ANSI colors, symbols, and Japanese)
    stdout = (
        "TestHelper: UserManager found, attempting authentication\n"
        "  ✘    1 [basic] › e2e/basic/00-tst-outliner-visible-after-prepare-0f1a2b3c.spec.ts:15:5 › 見出し\n"
        "\x1b[31mTesting stopped early after 1 maximum allowed failures.\x1b[39m\n\n"
        "  1) [basic] › e2e/basic/00-tst-outliner-visible-after-prepare-0f1a2b3c.spec.ts:15:5 › 見出し \n\n"
        "    at /home/ubuntu/src3/outliner/client/e2e/basic/00-tst-outliner-visible-after-prepare-0f1a2b3c.spec.ts:11:10\n"
    )
    stderr = ""
    path = extract_first_failed_test(stdout, stderr)
    assert path == "e2e/basic/00-tst-outliner-visible-after-prepare-0f1a2b3c.spec.ts"


def test_playwright_prefers_fail_over_pass_when_both_present():
    # Return failing side even when success line comes first followed by fail line
    stdout = "  \u2713    1 [basic] \u203a e2e/basic/pass-example.spec.ts:10:5 \u203a ok \n" "  \u2718   22 [new] \u203a e2e/new/fail-example.spec.ts:20:5 \u203a ng \n" "\n  1) [new] \u203a e2e/new/fail-example.spec.ts:20:5 \u203a ng \n"
    stderr = ""
    path = extract_first_failed_test(stdout, stderr)
    assert path == "e2e/new/fail-example.spec.ts"


def test_stderr_is_prioritized_over_stdout(monkeypatch):
    stdout = "  \u2713    1 [basic] › e2e/basic/pass-example.spec.ts:10:5 › ok\n" "All tests passed\n"
    stderr = "  ✘    1 [new] › e2e/new/fail-example.spec.ts:20:5 › broken\n"

    failing_path = "e2e/new/fail-example.spec.ts"

    monkeypatch.setattr(
        os.path,
        "exists",
        lambda p: p == failing_path,
    )

    path = extract_first_failed_test(stdout, stderr)
    assert path == failing_path


def test_vitest_fail_line_extracts_test_ts(monkeypatch):
    stdout = "  ✓ 22 tests passed"
    stderr = textwrap.dedent(
        """
        2025-09-26 17:55:46.482 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming - ⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯
        2025-09-26 17:55:46.483 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming -
        2025-09-26 17:55:46.483 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming -  FAIL  |unit| src/tests/attachmentService.test.ts > attachmentService > listAttachments requires auth
        2025-09-26 17:55:46.484 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming - AssertionError: promise resolved "[]" instead of rejecting
        2025-09-26 17:55:46.485 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming -
        2025-09-26 17:55:46.495 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming -  ❯ src/tests/attachmentService.test.ts:10:58
        2025-09-26 17:55:46.499 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming -
        2025-09-26 17:55:46.501 | ERROR    | auto_coder/utils.py:161 in _run_with_streaming - ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯
        """
    )

    target = "src/tests/attachmentService.test.ts"

    monkeypatch.setattr(os.path, "exists", lambda p: p == target)

    path = extract_first_failed_test(stdout, stderr)
    assert path == target


def test_vitest_success_playwright_fail_should_detect_playwright(monkeypatch):
    """When vitest succeeds and playwright fails, detect playwright's failure"""
    stdout = textwrap.dedent(
        """
        ✓ src/tests/unit/foo.test.ts (5 tests) 125ms
        ✓ src/tests/unit/bar.test.ts (3 tests) 89ms

        Test Files  2 passed (2)
             Tests  8 passed (8)
          Start at  10:30:15
          Duration  1.2s

        Running Playwright tests...

          ✘    1 [basic] › e2e/basic/login.spec.ts:20:5 › should login successfully

          1) [basic] › e2e/basic/login.spec.ts:20:5 › should login successfully

             Error: expect(received).toContain(expected)

             Expected substring: "Welcome"
             Received string: "Error: Invalid credentials"
        """
    )
    stderr = ""

    expected_path = "e2e/basic/login.spec.ts"
    monkeypatch.setattr(os.path, "exists", lambda p: p == expected_path)

    path = extract_first_failed_test(stdout, stderr)
    assert path == expected_path


def test_playwright_success_vitest_fail_should_detect_vitest(monkeypatch):
    """When playwright succeeds and vitest fails, detect vitest's failure"""
    stdout = textwrap.dedent(
        """
        Running Playwright tests...

          ✓ [basic] › e2e/basic/login.spec.ts:20:5 › should login successfully
          ✓ [basic] › e2e/basic/signup.spec.ts:15:5 › should signup successfully

          2 passed (2.5s)

        Running Vitest...

         FAIL  src/tests/unit/auth.test.ts > auth > should validate token
        AssertionError: expected false to be true

         ❯ src/tests/unit/auth.test.ts:25:10

        Test Files  1 failed (1)
             Tests  1 failed (1)
        """
    )
    stderr = ""

    expected_path = "src/tests/unit/auth.test.ts"
    monkeypatch.setattr(os.path, "exists", lambda p: p == expected_path)

    path = extract_first_failed_test(stdout, stderr)
    assert path == expected_path


def test_pytest_success_playwright_fail_should_detect_playwright(monkeypatch):
    """When pytest succeeds and playwright fails, detect playwright's failure"""
    stdout = textwrap.dedent(
        """
        ============================= test session starts ==============================
        collected 15 items

        tests/test_api.py ............... [100%]

        ============================== 15 passed in 2.34s ==============================

        Running Playwright tests...

          ✘    1 [core] › e2e/core/api-integration.spec.ts:30:5 › API integration test

          1) [core] › e2e/core/api-integration.spec.ts:30:5 › API integration test

             Error: Timeout 5000ms exceeded
        """
    )
    stderr = ""

    expected_path = "e2e/core/api-integration.spec.ts"
    monkeypatch.setattr(os.path, "exists", lambda p: p == expected_path)

    path = extract_first_failed_test(stdout, stderr)
    assert path == expected_path
