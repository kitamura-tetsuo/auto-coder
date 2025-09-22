"""
Test runner functionality for Auto-Coder automation engine.
"""

import math
import os
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

from .utils import CommandExecutor, change_fraction, slice_relevant_error_window, extract_first_failed_test, log_action
from .automation_config import AutomationConfig
from .logger_config import get_logger

logger = get_logger(__name__)
cmd = CommandExecutor()


def cleanup_llm_task_file(path: str = "./llm_task.md") -> None:
    """Remove the LLM task log file before committing final fixes."""
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.debug(f"Removed {path} prior to commit")
    except Exception as exc:
        logger.warning(f"Failed to remove {path}: {exc}")


def run_local_tests(config: AutomationConfig, test_file: Optional[str] = None) -> Dict[str, Any]:
    """Run local tests using configured script or pytest fallback.

    If test_file is specified, only that test file will be run.
    Otherwise, all tests will be run.

    Returns a dict: {success, output, errors, return_code}
    """
    try:
        # If a specific test file is specified, run only that test (always via TEST_SCRIPT_PATH)
        if test_file:
            logger.info(f"Running only the specified test file via script: {test_file}")
            cmd_list = ['bash', config.TEST_SCRIPT_PATH, test_file]
            result = cmd.run_command(cmd_list, timeout=cmd.DEFAULT_TIMEOUTS['test'])
            return {
                'success': result.success,
                'output': result.stdout,
                'errors': result.stderr,
                'return_code': result.returncode,
                'command': ' '.join(cmd_list),
                'test_file': test_file,
            }

        # Always run via test script
        cmd_list = ['bash', config.TEST_SCRIPT_PATH]
        logger.info(f"Running local tests via script: {config.TEST_SCRIPT_PATH}")
        result = cmd.run_command(cmd_list, timeout=cmd.DEFAULT_TIMEOUTS['test'])

        # If the test run failed, try to extract the first failed test file and run it via the script
        if not result.success:
            # Extract the first failed test file from the output
            first_failed_test = extract_first_failed_test(result.stdout, result.stderr)
            if first_failed_test:
                return run_local_tests(config, test_file=first_failed_test)

        return {
            'success': result.success,
            'output': result.stdout,
            'errors': result.stderr,
            'return_code': result.returncode,
            'command': ' '.join(cmd_list),
            'test_file': None,
        }
    except Exception as e:
        logger.error(f"Local test execution failed: {e}")
        return {
            'success': False,
            'output': '',
            'errors': str(e),
            'return_code': -1,
            'command': '',
            'test_file': None,
        }


def apply_workspace_test_fix(config: AutomationConfig, test_result: Dict[str, Any], llm_client, dry_run: bool = False) -> str:
    """Ask the LLM to apply workspace edits based on local test failures.

    Returns a short action summary string.
    """
    try:
        error_summary = extract_important_errors(test_result)
        if not error_summary:
            return "No actionable errors found in local test output"

        fix_prompt = f"""
You are operating directly in this repository workspace with write access.

Goal: Make local tests pass by applying safe edits.

Task Memory File: ./llm_task.md
- If the file exists, read it first to understand previous purpose/method/result notes.
- After deciding on the fix, update the file with concise Markdown sections for Purpose, Method, and Result describing this attempt.
- Ensure the file always reflects the latest state when you finish (overwrite rather than append blindly).

STRICT RULES:
- Do NOT run git commit/push; the system will handle that.
- Prefer the smallest change that resolves failures and preserves intent.

Local Test Failure Summary (truncated):
{error_summary[: config.MAX_PROMPT_SIZE]}

 Local test command used:
 {test_result.get('command', 'pytest -q --maxfail=1')}

Now apply the fix directly in the repository.
Run tests again after applying the fix to verify that the fix resolves the issue.
Return a single concise line summarizing the change.
"""

        if dry_run:
            return "[DRY RUN] Would apply fixes for local test failures"

        # Use the LLM client/manager to run the prompt
        if hasattr(llm_client, 'run_test_fix_prompt') and callable(getattr(llm_client, 'run_test_fix_prompt')):
            response = llm_client.run_test_fix_prompt(fix_prompt)
        else:
            response = llm_client._run_llm_cli(fix_prompt)
        if response and response.strip():
            # Take the first line as the summary and trim
            first_line = response.strip().splitlines()[0]
            return first_line[: config.MAX_RESPONSE_SIZE]
        return "LLM produced no response"
    except Exception as e:
        return f"Error applying workspace test fix: {e}"


def fix_to_pass_tests(config: AutomationConfig, dry_run: bool = False, max_attempts: Optional[int] = None, llm_client=None) -> Dict[str, Any]:
    """Run tests and, if failing, repeatedly request LLM fixes until tests pass.

    If the LLM makes no edits (no changes to commit) in an iteration, raise an error and stop.
    Returns a summary dict.
    """
    attempts_limit = max_attempts if isinstance(max_attempts, int) and max_attempts > 0 else config.MAX_FIX_ATTEMPTS
    summary: Dict[str, Any] = {
        'mode': 'fix-to-pass-tests',
        'attempts': 0,
        'success': False,
        'messages': []
    }

    # Track previous test output and the error summary given to LLM (from last completed test run)
    prev_full_output: Optional[str] = None
    prev_error_summary: Optional[str] = None

    # Cache the latest post-fix test result to avoid redundant runs in the next loop
    cached_test_result: Optional[Dict[str, Any]] = None

    # Track the test file that is currently being fixed
    current_test_file: Optional[str] = None

    # Support infinite attempts (math.inf) by using a while loop
    attempt = 0  # counts actual test executions
    while True:
        # Use cached result (from previous post-fix run) if available; otherwise run tests now
        if cached_test_result is not None:
            test_result = cached_test_result
            cached_test_result = None
        else:
            attempt += 1
            summary['attempts'] = attempt
            logger.info(f"Running local tests (attempt {attempt}/{attempts_limit})")
            test_result = run_local_tests(config, test_file=current_test_file)
            # Update the current test file being fixed
            current_test_file = test_result.get('test_file')
        if test_result['success']:
            msg = f"Local tests passed on attempt {attempt}"
            logger.info(msg)
            summary['messages'].append(msg)
            summary['success'] = True
            if not dry_run:
                cleanup_llm_task_file()
            return summary

        # Apply LLM-based fix
        action_msg = apply_workspace_test_fix(config, test_result, llm_client, dry_run)
        summary['messages'].append(action_msg)

        if dry_run:
            # In dry-run we do not commit; just continue attempts
            continue

        # Baseline (pre-fix) outputs for comparison
        baseline_full_output = f"{test_result.get('errors', '')}\n{test_result.get('output', '')}".strip()
        baseline_error_summary = extract_important_errors(test_result)

        # Re-run tests AFTER LLM edits to measure change and decide commit
        attempt += 1
        summary['attempts'] = attempt
        logger.info(f"Re-running local tests after LLM fix (attempt {attempt}/{attempts_limit})")
        post_result = run_local_tests(config, test_file=current_test_file)

        post_full_output = f"{post_result.get('errors', '')}\n{post_result.get('output', '')}".strip()
        post_error_summary = extract_important_errors(post_result)

        # Update previous context for next loop start
        prev_full_output = post_full_output
        prev_error_summary = post_error_summary

        cleanup_pending = False

        if post_result['success']:
            # Tests passed after the fix; proceed to commit
            pass_msg = f"Local tests passed on attempt {attempt}"
            logger.info(pass_msg)
            summary['messages'].append(pass_msg)
            should_commit = True
            if not dry_run:
                cleanup_pending = True
        else:
            # Compute change ratios between pre-fix and post-fix results
            try:
                change_ratio_tests = change_fraction(baseline_full_output or "", post_full_output or "")
                change_ratio_errors = change_fraction(baseline_error_summary or "", post_error_summary or "")
                max_change = max(change_ratio_tests, change_ratio_errors)
            except Exception:
                max_change = 1.0  # default to commit if comparison fails

            if max_change < 0.10:
                # Consider this as insufficient change; skip commit and ask LLM again next loop
                info = "Change below 10% threshold; skipping commit and retrying"
                logger.info(info)
                summary['messages'].append(info)
                # Use this post-fix test result as the starting point for the next loop
                cached_test_result = post_result
                # Stop if finite limit reached
                try:
                    if isinstance(attempts_limit, (int, float)) and math.isfinite(float(attempts_limit)) and attempt >= int(attempts_limit):
                        break
                except Exception:
                    pass
                # Continue to next loop (will request another LLM fix)
                continue
            else:
                info = f"Significant change detected ({max_change:.2%}); committing and continuing"
                logger.info(info)
                summary['messages'].append(info)
                should_commit = True
                
        current_test_file = None

        if cleanup_pending:
            cleanup_llm_task_file()

        # Stage and commit; detect 'no changes' as an immediate error per requirement
        add_res = cmd.run_command(['git', 'add', '.'])
        if not add_res.success:
            errmsg = f"Failed to stage changes: {add_res.stderr}"
            logger.error(errmsg)
            summary['messages'].append(errmsg)
            break

        # Ask LLM to craft a clear, concise commit message for the applied change
        commit_msg = generate_commit_message_via_llm(
            config=config,
            error_summary=post_error_summary,
            action_summary=action_msg,
            attempt=attempt,
            llm_client=llm_client,
        )
        if not commit_msg:
            commit_msg = format_commit_message(config, action_msg, attempt)
            
        # If tests passed, mark success and return
        if post_result['success']:
            summary['success'] = True
            return summary

        # Cache the failing post-fix result for the next loop to avoid re-running before LLM edits
        cached_test_result = post_result

        # Stop if finite limit reached
        try:
            if isinstance(attempts_limit, (int, float)) and math.isfinite(float(attempts_limit)) and attempt >= int(attempts_limit):
                break
        except Exception:
            # If attempts_limit is not a number, treat as unlimited
            pass

    # Final test after exhausting attempts (optional): do not re-run here because we already
    # executed a post-fix run within the loop. Keep messages concise.
    summary['messages'].append("Local tests still failing after attempts")

    return summary


def generate_commit_message_via_llm(config: AutomationConfig, error_summary: str, action_summary: str, attempt: int, llm_client) -> str:
    """Use LLM to generate a concise commit message based on the fix context.

    Keeps the call minimal and instructs the model to output a single-line subject only.
    Never asks the LLM to run git commands.
    """
    try:
        if llm_client is None:
            return ""

        prompt = f"""
You have just applied code changes to fix failing local tests.
Craft a concise commit subject (single line, <= 72 chars, imperative mood) summarizing the change.

Rules:
- Subject line only. No body, no backticks, no code blocks.
- Do NOT include commands like git commit/push.
- Prefer specifics (file/function/test) if clear from context.

Context:
- Action summary from previous step: {action_summary.strip()[:200]}
- Error summary hint (truncated):
{(error_summary or '').strip()[:400]}
"""

        response = llm_client._run_llm_cli(prompt)
        if not response:
            return ""
        # Take first non-empty line, sanitize length
        for line in response.splitlines():
            line = line.strip().strip('"').strip("'")
            if line:
                if len(line) > 72:
                    line = line[:72].rstrip()
                return f"Auto-Coder: {line}"
        return ""
    except Exception:
        return ""


def format_commit_message(config: AutomationConfig, llm_summary: str, attempt: int) -> str:
    """Create a concise commit message using the LLM-produced summary.

    - Prefix with "Auto-Coder:" to unify automation commits
    - Sanitize dry-run prefixes and trim to a reasonable length
    - Fallback to a generic message if empty
    """
    base = (llm_summary or "").strip()
    # Remove any dry-run indicator if accidentally present
    if base.startswith('[DRY RUN]'):
        base = base[len('[DRY RUN]'):].strip()
    if not base:
        base = "Fix local tests"
    # Limit length to ~100 chars
    if len(base) > 100:
        base = base[:100].rstrip()
    return f"Auto-Coder: {base}"


def extract_important_errors(test_result: Dict[str, Any]) -> str:
    """Extract important error information from test output.

    改良点:
    - Playwright 形式の失敗ブロック（"Error:   1) [suite] › ... .spec.ts ..."）を優先的に広めのコンテキストで抽出
    - 期待/受領や該当 expect 行、"X failed" サマリを含めやすくする
    """
    if test_result['success']:
        return ""

    errors = test_result.get('errors', '')
    output = test_result.get('output', '')

    # Combine stderr and stdout
    full_output = f"{errors}\n{output}".strip()

    if not full_output:
        return "Tests failed but no error output available"

    lines = full_output.split('\n')

    # 0) 期待/受領（Playwright/Jest）の詳細行が含まれていれば、見出しからその周辺を優先抽出
    if ('Expected substring:' in full_output) or ('Received string:' in full_output) or ('expect(received)' in full_output):
        try:
            import re
            # 見出し候補を後方に向かって探す
            # Playwright 見出し: 先頭に "Error:" がないケースや、先頭空白/× 記号を許容
            hdr_pat = re.compile(r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\).*\.spec\.ts:.*")
            idx_expect = None
            for i, ln in enumerate(lines):
                if ('Expected substring:' in ln) or ('Received string:' in ln) or ('expect(received)' in ln):
                    idx_expect = i
                    break
            if idx_expect is not None:
                start = 0
                for j in range(idx_expect, -1, -1):
                    if hdr_pat.search(lines[j]):
                        start = j
                        break
                end = min(len(lines), idx_expect + 60)
                block = '\n'.join(lines[start:end])
                if block:
                    return block
        except Exception:
            pass

    # 1) Playwright の典型パターンを優先抽出
    try:
        import re
        # 失敗見出し: "Error:   1) [suite] › e2e/... .spec.ts:line:col › ..."
        header_indices = []
        # Playwright 見出し: 先頭に "Error:" がない/ある両方、先頭空白や × 記号も許容
        header_regex = re.compile(
            r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+\[[^\]]+\]\s+\u203a\s+.*\.spec\.ts:\d+:\d+\s+\u203a\s+.*|"
            r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+.*\.spec\.ts:.*",
            re.UNICODE,
        )
        for idx, ln in enumerate(lines):
            if header_regex.search(ln):
                header_indices.append(idx)
        # 期待/受領の典型
        expect_regex = re.compile(r"expect\(received\).*|Expected substring:|Received string:")

        blocks = []
        for start_idx in header_indices:
            end_idx = min(len(lines), start_idx + 120)  # 広めに120行
            # 次のエラー見出しで打ち切り（空行では打ち切らない）
            for j in range(start_idx + 1, min(len(lines), start_idx + 300)):
                if j >= len(lines):
                    break
                s = lines[j]
                if header_regex.search(s):
                    end_idx = j
                    break
            block = lines[start_idx:end_idx]
            # 期待/受領や該当 expect 行が含まれているかチェック
            if any(expect_regex.search(b) for b in block) or any('.spec.ts' in b for b in block):
                blocks.append('\n'.join(block))
        if blocks:
            result = '\n\n'.join(blocks)
            # 期待/受領の行が含まれていなければ追補する
            if 'Expected substring:' not in result or 'Received string:' not in result:
                extra_lines = []
                for i, ln in enumerate(lines):
                    if 'Expected substring:' in ln or 'Received string:' in ln or 'expect(received)' in ln:
                        start = max(0, i - 2)
                        end = min(len(lines), i + 4)
                        extra_lines.extend(lines[start:end])
                if extra_lines:
                    result = result + "\n\n--- Expectation Details ---\n" + '\n'.join(extra_lines)
            if len(result) > 3000:
                result = result[:3000] + "\n... (output truncated)"
            return result
    except Exception:
        pass

    # 2) キーワードベースのフォールバック抽出（従来ロジックを改善）
    important_lines = []
    # Keywords that indicate important error information
    error_keywords = [
        # error detection
        'error:', 'Error:', 'ERROR:', 'error',
        # failed detection
        'failed:', 'Failed:', 'FAILED:', 'failed',
        # exceptions and traces
        'exception:', 'Exception:', 'EXCEPTION:',
        'traceback:', 'Traceback:', 'TRACEBACK:',
        # assertions and common python errors
        'assertion', 'Assertion', 'ASSERTION',
        'syntax error', 'SyntaxError',
        'import error', 'ImportError',
        'module not found', 'ModuleNotFoundError',
        'test failed', 'Test failed', 'TEST FAILED',
        # e2e / Playwright related
        'e2e/', '.spec.ts', 'playwright'
    ]

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(keyword.lower() in line_lower for keyword in error_keywords):
            # もう少し広めに文脈を抽出
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
    result = '\n'.join(unique_lines)
    if len(result) > 2000:
        result = result[:2000] + "\n... (output truncated)"

    return result if result else "Tests failed but no specific error information found"


def run_pr_tests(config: AutomationConfig, pr_data: Dict[str, Any]) -> Dict[str, Any]:
    """Run tests for a PR and return results."""
    pr_number = pr_data['number']

    try:
        log_action(f"Running tests for PR #{pr_number}")
        result = run_local_tests(config)
        log_action(f"Test result for PR #{pr_number}: {'PASS' if result['success'] else 'FAIL'}")
        return result

    except Exception as e:
        error_msg = f"Error running tests for PR #{pr_number}: {e}"
        logger.error(error_msg)
        return {
            'success': False,
            'output': '',
            'errors': error_msg,
            'return_code': -1
        }
