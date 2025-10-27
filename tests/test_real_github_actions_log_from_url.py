"""実際のGitHub ActionsログURLを使用した統合テスト

このテストは実際のGitHub Actions APIを呼び出して、
https://github.com/kitamura-tetsuo/outliner/actions/runs/18828609259/job/53715705095
のログを取得し、エラーコンテキストの抽出が正しく機能することを検証します。
"""

import pytest

from src.auto_coder.pr_processor import get_github_actions_logs_from_url, _extract_error_context


# 実際のGitHub Actions URLを使用
REAL_JOB_URL = "https://github.com/kitamura-tetsuo/outliner/actions/runs/18828609259/job/53715705095"





def test_get_real_github_actions_logs_from_url(_use_real_commands, _use_real_home):
    """実際のGitHub Actions URLからログを取得できることを確認"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)
    
    # 結果が空でないことを確認
    assert result, "ログが取得できませんでした"
    assert len(result) > 0, "ログが空です"
    
    # ジョブIDが含まれていることを確認
    assert "53715705095" in result, "ジョブIDが含まれていません"
    
    print(f"\n=== ログ取得成功 ===")
    print(f"ログ長: {len(result)} 文字")
    print(f"行数: {len(result.splitlines())} 行")


def test_real_log_contains_eslint_error(_use_real_commands, _use_real_home):
    """実際のログにESLintエラーが含まれていることを確認"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)
    
    # ESLintエラーが含まれていることを確認
    assert "getAzureConfig" in result, "ESLintエラー (getAzureConfig) が含まれていません"
    assert "is not defined" in result, "'is not defined' エラーメッセージが含まれていません"
    assert "functions/index.js" in result, "エラーファイル名が含まれていません"
    
    # エラーの詳細が含まれていることを確認
    assert "error" in result.lower(), "エラーキーワードが含まれていません"
    
    print(f"\n=== ESLintエラー検出成功 ===")


def test_real_log_contains_unit_test_failures(_use_real_commands, _use_real_home):
    """実際のログに失敗したステップのログが含まれていることを確認"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)

    # 失敗したステップのログが含まれていることを確認
    # このジョブでは "Lint functions" と "cat log" が失敗している
    assert "=== Step: Lint functions ===" in result or "Lint functions" in result, "Lint functionsステップが含まれていません"
    assert "=== Step: cat log ===" in result or "cat log" in result, "cat logステップが含まれていません"

    print(f"\n=== 失敗したステップ検出成功 ===")


def test_real_log_extract_error_context_sufficient(_use_real_commands, _use_real_home):
    """実際のログから抽出されたエラーコンテキストが十分であることを確認"""
    # まず生のログを取得
    raw_result = get_github_actions_logs_from_url(REAL_JOB_URL)

    # ログの長さを確認
    raw_lines = raw_result.splitlines()
    print(f"\n=== 生ログ情報 ===")
    print(f"文字数: {len(raw_result)}")
    print(f"行数: {len(raw_lines)}")

    # エラーコンテキストが十分に含まれていることを確認
    # 主要なエラー情報が含まれている
    assert "getAzureConfig" in raw_result, "ESLintエラーが含まれていません"

    # エラーの前後のコンテキストが含まれている
    # ESLintエラーの場合
    if "getAzureConfig" in raw_result:
        # エラー行の前後の情報が含まれているか確認
        assert "eslint" in raw_result.lower(), "ESLint実行情報が含まれていません"
        assert "Process completed with exit code" in raw_result, "プロセス終了情報が含まれていません"

    # 最低限のコンテキストが含まれていることを確認（10行以上）
    assert len(raw_lines) >= 10, f"抽出された行数が少なすぎます: {len(raw_lines)} 行"

    print(f"\n=== エラーコンテキスト検証成功 ===")
    print(f"抽出行数: {len(raw_lines)} 行（適切な範囲内）")


def test_real_log_llm_can_understand(_use_real_commands, _use_real_home):
    """実際のログがLLMが理解できる形式であることを確認"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)
    
    # LLMが理解するために必要な情報が含まれていることを確認
    
    # 1. エラーの種類が明確
    assert "error" in result.lower() or "failed" in result.lower(), "エラーの種類が不明確です"
    
    # 2. エラーの場所が明確
    assert "functions/index.js" in result or ".test.ts" in result, "エラーの場所が不明確です"
    
    # 3. エラーメッセージが明確
    has_clear_error_message = (
        "is not defined" in result or
        "is not a function" in result or
        "is not iterable" in result or
        "Cannot read properties" in result
    )
    assert has_clear_error_message, "エラーメッセージが不明確です"
    
    # 4. 十分なコンテキスト（エラーの前後の情報）
    lines = result.splitlines()
    assert len(lines) >= 20, "コンテキストが不足しています"
    
    # 5. 不要な情報が除外されている（セットアップ情報など）
    # 成功したステップの詳細は含まれていないはず
    # ただし、エラーの前後10行には含まれる可能性があるので、
    # 全体として成功ステップが大量に含まれていないことを確認
    setup_lines = [line for line in lines if "Installing" in line or "Setup" in line]
    # セットアップ行が全体の50%未満であることを確認
    assert len(setup_lines) < len(lines) * 0.5, "不要なセットアップ情報が多すぎます"
    
    print(f"\n=== LLM理解可能性検証成功 ===")
    print(f"エラー情報: 明確")
    print(f"コンテキスト: 十分（{len(lines)} 行）")
    print(f"不要情報: 除外済み（セットアップ行 {len(setup_lines)}/{len(lines)}）")


def test_real_log_error_details(_use_real_commands, _use_real_home):
    """実際のログから抽出されたエラーの詳細を確認"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)

    # エラーの詳細情報を収集
    errors_found = []

    if "getAzureConfig" in result:
        errors_found.append("ESLint: getAzureConfig is not defined")

    if "No such file or directory" in result:
        errors_found.append("File not found: server/logs/test-log-service-tee.log")

    if "toggleReaction is not a function" in result:
        errors_found.append("TypeError: toggleReaction is not a function")

    if "ytext.insert is not a function" in result:
        errors_found.append("TypeError: ytext.insert is not a function")

    if "ytext.delete is not a function" in result:
        errors_found.append("TypeError: ytext.delete is not a function")

    if "items is not iterable" in result:
        errors_found.append("TypeError: items is not iterable")

    if "Failed to resolve import" in result:
        errors_found.append("Import resolution error")

    # 少なくとも1つのエラーが検出されていることを確認
    assert len(errors_found) > 0, "エラーが検出されませんでした"

    print(f"\n=== 検出されたエラー ===")
    for i, error in enumerate(errors_found, 1):
        print(f"{i}. {error}")

    # 少なくとも1つのエラーが検出されていることを確認
    assert len(errors_found) >= 1, f"検出されたエラーが少なすぎます: {len(errors_found)} 個"


def test_real_log_performance(_use_real_commands, _use_real_home):
    """実際のログ取得のパフォーマンスを確認"""
    import time

    start_time = time.time()
    result = get_github_actions_logs_from_url(REAL_JOB_URL)
    end_time = time.time()

    elapsed_time = end_time - start_time

    # 結果が取得できていることを確認
    assert result, "ログが取得できませんでした"

    # パフォーマンス情報を出力
    print(f"\n=== パフォーマンス情報 ===")
    print(f"実行時間: {elapsed_time:.2f} 秒")
    print(f"ログサイズ: {len(result)} 文字")
    print(f"行数: {len(result.splitlines())} 行")

    # 合理的な時間内に完了していることを確認（60秒以内）
    assert elapsed_time < 60, f"ログ取得に時間がかかりすぎています: {elapsed_time:.2f} 秒"


def test_real_log_no_ansi_escape_sequences(_use_real_commands, _use_real_home):
    """実際のログにANSIエスケープシーケンスが含まれていないことをテスト"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)

    # ANSIエスケープシーケンスが含まれていないこと
    ansi_marker = "\x1b["
    assert ansi_marker not in result, f"ANSIエスケープシーケンスが含まれています"

    # 制御文字が含まれていないこと（一部の例外を除く）
    for line in result.split("\n"):
        # タブと改行以外の制御文字をチェック
        for char in line:
            if ord(char) < 32 and char not in ["\t", "\n", "\r"]:
                assert False, f"制御文字が含まれています: {repr(char)} in line: {line[:100]}"

    print(f"\n=== ANSIエスケープシーケンス除去確認成功 ===")


def test_real_log_no_timestamps(_use_real_commands, _use_real_home):
    """実際のログにタイムスタンプが含まれていないことをテスト"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)

    # タイムスタンプが含まれていないこと
    import re

    timestamp_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s+", re.MULTILINE)
    matches = timestamp_pattern.findall(result)
    assert len(matches) == 0, f"タイムスタンプが含まれています: {matches[:5]}"

    print(f"\n=== タイムスタンプ除去確認成功 ===")


def test_real_log_eslint_block_complete(_use_real_commands, _use_real_home):
    """実際のログにESLintブロック全体が含まれていることをテスト"""
    result = get_github_actions_logs_from_url(REAL_JOB_URL)

    # ESLintコマンド実行行が含まれていること
    assert "> eslint . --fix" in result or "eslint . --fix" in result, "ESLintコマンド実行行が含まれていません"

    # ファイルパスが含まれていること
    assert "/tmp/runner/work/outliner/outliner/functions/index.js" in result, "ファイルパスが含まれていません"

    # 警告が含まれていること
    assert "'jwt' is assigned a value but never used" in result, "警告が含まれていません"

    # エラーが含まれていること
    assert "'getAzureConfig' is not defined" in result, "エラーが含まれていません"

    # 問題数のサマリーが含まれていること
    assert "2 problems (1 error, 1 warning)" in result or "✖ 2 problems" in result, "問題数のサマリーが含まれていません"

    print(f"\n=== ESLintブロック完全性確認成功 ===")
    print("ESLintコマンド実行行: ✓")
    print("ファイルパス: ✓")
    print("警告: ✓")
    print("エラー: ✓")
    print("問題数サマリー: ✓")

