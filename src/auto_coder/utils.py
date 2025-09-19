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
    """テスト出力から「最初に失敗したテストファイルのパス」を抽出して返す。

    対応フォーマット:
    - pytest: 末尾サマリの "FAILED tests/test_x.py::test_y - ..." など
    - pytest: トレースバック中の "tests/test_x.py:123: in test_y" など
    - Playwright: 任意ログ中の "e2e/foo/bar.spec.ts:16:5" など

    見つかったパスを返す。実在確認に失敗しても候補があれば返すことがある（呼び出し側で解釈するため）。
    """
    import re

    # stdout/stderr を結合して解析
    full_output = f"{stdout}\n{stderr}"

    # ANSIカラーコードを除去
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    full_output = ansi_escape.sub("", full_output)

    candidates: list[str] = []

    # 1) pytest の FAILED サマリ行から抽出
    #   例: "FAILED tests/test_foo.py::test_bar - AssertionError ..."
    #        → 最初の .py までをファイルパスとして取得
    for pat in [
        r"^FAILED\s+([^\s:]+\.py)::",           # :: が続く一般的な nodeid
        r"^FAILED\s+([^\s:]+\.py)\s*[-:]",     # ハイフン/コロンで続く系
        r"^FAILED\s+([^\s:]+\.py)\b",          # 念のためのフォールバック
    ]:
        m = re.search(pat, full_output, re.MULTILINE)
        if m:
            candidates.append(m.group(1))
            break

    # 2) pytest のトレースバック行から tests/ 配下の .py を抽出
    #   例: "tests/test_foo.py:12: in test_bar" → ファイル部分だけ
    m = re.search(r"(^|\s)((?:tests?/|^tests?/)[^:\s]+\.py):\d+", full_output, re.MULTILINE)
    if m:
        py_path = m.group(2)
        if py_path not in candidates:
            candidates.append(py_path)

    # 3) Playwright の失敗行から .spec.ts を抽出（成功行 ✓ を除外）
    #   優先度:
    #   a) 先頭に✘/×/x などの失敗マークがある list レポーター行
    #   b) 見出し形式の "1) [suite] › path.spec.ts:line:col › ..." 行
    #   c) 上記が無い場合のみ、最後に汎用的な .spec.ts 抽出をフォールバックとして試みる
    lines = full_output.split('\n')
    # a) 失敗マーク（✘/×/x/X）行
    fail_bullet_re = re.compile(r"^[^\S\r\n]*[✘×xX]\s+\d+\s+\[[^\]]+\]\s+›\s+([^\s:]+\.spec\.ts):\d+:\d+")
    # b) 見出し形式の番号行
    fail_heading_re = re.compile(r"^[^\S\r\n]*\d+\)\s+\[[^\]]+\]\s+›\s+([^\s:]+\.spec\.ts):\d+:\d+")

    for ln in lines:
        m = fail_bullet_re.search(ln)
        if m:
            spec_path = m.group(1)
            m_e2e = re.search(r"(?:^|/)(e2e/[A-Za-z0-9_./-]+\.spec\.ts)$", spec_path)
            norm = m_e2e.group(1) if m_e2e else spec_path
            if norm not in candidates:
                candidates.append(norm)

    for ln in lines:
        m = fail_heading_re.search(ln)
        if m:
            spec_path = m.group(1)
            m_e2e = re.search(r"(?:^|/)(e2e/[A-Za-z0-9_./-]+\.spec\.ts)$", spec_path)
            norm = m_e2e.group(1) if m_e2e else spec_path
            if norm not in candidates:
                candidates.append(norm)

    # c) フォールバック（失敗専用パターンが何も当たらなかった場合のみ）
    if not candidates:
        for spec_path in re.findall(r"([^\s:]+\.spec\.ts)", full_output):
            m_e2e = re.search(r"(?:^|/)(e2e/[A-Za-z0-9_./-]+\.spec\.ts)$", spec_path)
            norm = m_e2e.group(1) if m_e2e else spec_path
            if norm not in candidates:
                candidates.append(norm)

    # 実在確認して返す（見つからなければ最初の候補を返す）
    for path in candidates:
        if os.path.exists(path):
            return path
    # 実在しない環境（例えば別リポジトリの作業ディレクトリで生成されたパス）でも
    # 呼び出し側で扱えるよう、候補があれば返す
    if candidates:
        return candidates[0]

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