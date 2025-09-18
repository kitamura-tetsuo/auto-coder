"""
Utility classes for Auto-Coder automation engine.
"""

import subprocess
import os
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

from .logger_config import get_logger

logger = get_logger(__name__)


@dataclass
class CommandResult:
    """Result of a command execution."""
    success: bool
    stdout: str
    stderr: str
    returncode: int


class CommandExecutor:
    """Utility class for executing commands with consistent error handling."""

    # Default timeouts for different command types
    DEFAULT_TIMEOUTS = {
        'git': 120,
        'gh': 60,
        'test': 3600,
        'default': 60
    }

    @classmethod
    def run_command(
        cls,
        cmd: List[str],
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        check_success: bool = True
    ) -> CommandResult:
        """Run a command with consistent error handling."""
        if timeout is None:
            # Auto-detect timeout based on command type
            cmd_type = cmd[0] if cmd else 'default'
            timeout = cls.DEFAULT_TIMEOUTS.get(cmd_type, cls.DEFAULT_TIMEOUTS['default'])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd
            )

            success = result.returncode == 0 if check_success else True

            return CommandResult(
                success=success,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                returncode=-1
            )
        except Exception as e:
            logger.error(f"Command execution failed: {' '.join(cmd)}: {e}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=str(e),
                returncode=-1
            )


def change_fraction(old: str, new: str) -> float:
    """Return fraction of change between two strings (0.0..1.0).

    Uses difflib.SequenceMatcher similarity; change = 1 - ratio.
    """
    try:
        import difflib
        if old is None and new is None:
            return 0.0
        old_s = old or ""
        new_s = new or ""
        if old_s == new_s:
            return 0.0
        ratio = difflib.SequenceMatcher(None, old_s, new_s).ratio()
        return max(0.0, 1.0 - ratio)
    except Exception:
        # Conservative fallback: assume large change
        return 1.0


def slice_relevant_error_window(text: str) -> str:
    """エラー関連の必要部分のみを返す（プレリュード切捨て＋後半重視・短縮）。
    方針:
    - 末尾側から優先トリガを探索し、その少し前から末尾までを返す
    - 見つからない場合は末尾の数百行に限定
    """
    if not text:
        return text
    lines = text.split('\n')
    # 優先度の高い順でグルーピング
    priority_groups = [
        ['Expected substring:', 'Received string:', 'expect(received)'],
        ['Error:   ', '.spec.ts', '##[error]'],
        ['Command failed with exit code', 'Process completed with exit code'],
        ['error was not a part of any test', 'Notice:', '##[notice]', 'notice'],
    ]
    start_idx = None
    # 末尾から優先トリガを探索
    for group in priority_groups:
        for i in range(len(lines) - 1, -1, -1):
            low = lines[i].lower()
            if any(g.lower() in low for g in group):
                start_idx = max(0, i - 30)
                break
        if start_idx is not None:
            break
    if start_idx is None:
        # トリガが無ければ、末尾のみ（最大300行）
        return '\n'.join(lines[-300:])
    # 末尾はそのまま。さらに最大800行に制限
    sliced = lines[start_idx:]
    if len(sliced) > 800:
        sliced = sliced[:800]
    return '\n'.join(sliced)


def extract_first_failed_test(stdout: str, stderr: str) -> Optional[str]:
    """Extract the first failed test file from the test output.
    
    Supports both pytest-style and Playwright-style test failures.
    """
    # Combine stdout and stderr for analysis
    full_output = f"{stdout}\n{stderr}"
    
    # Look for pytest-style failures (lines starting with "FAILED")
    import re
    failed_lines = re.findall(r"^FAILED\s+([^:]+):", full_output, re.MULTILINE)
    if failed_lines:
        first_failed_test = failed_lines[0]
        if os.path.exists(first_failed_test):
            return first_failed_test
    
    # Look for Playwright-style failures (lines containing .spec.ts)
    spec_lines = re.findall(r"([a-zA-Z0-9_/-]+\.spec\.ts)", full_output)
    if spec_lines:
        first_failed_test = spec_lines[0]
        if os.path.exists(first_failed_test):
            return first_failed_test
            
    return None


def log_action(action: str, success: bool = True, details: str = "") -> str:
    """Standardized action logging."""
    message = action
    if details:
        message += f": {details}"

    if success:
        logger.info(message)
    else:
        logger.error(message)
    return message