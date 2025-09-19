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
    # ANSIカラーや存在しないパスでも候補として返せること
    stdout = (
        "\x1b[31m  ✘    1 [basic] › e2e/basic/00-foo-bar.spec.ts:15:5 › title \x1b[39m\n"
        "\n  1) [basic] › e2e/basic/00-foo-bar.spec.ts:15:5 › title \n\n"
    )
    stderr = ""

    path = extract_first_failed_test(stdout, stderr)
    assert path == "e2e/basic/00-foo-bar.spec.ts"



def test_extract_playwright_from_sample_full_output_excerpt():
    # ユーザー提供のフォーマットに近い抜粋（ANSIカラーや記号・日本語含む）
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
    # 先に成功(\u2713)行があり、その後に失敗(\u2718)行が出るケースでも、失敗側を返す
    stdout = (
        "  \u2713    1 [basic] \u203a e2e/basic/pass-example.spec.ts:10:5 \u203a ok \n"
        "  \u2718   22 [new] \u203a e2e/new/fail-example.spec.ts:20:5 \u203a ng \n"
        "\n  1) [new] \u203a e2e/new/fail-example.spec.ts:20:5 \u203a ng \n"
    )
    stderr = ""
    path = extract_first_failed_test(stdout, stderr)
    assert path == "e2e/new/fail-example.spec.ts"
