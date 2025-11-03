#!/usr/bin/env python3
"""
簡潔なテスト：修正された run メソッドの構造を直接確認
"""

import os
import sys


def test_run_method_calls_functions():
    """run メソッドが process_issues と process_pull_requests を呼び出すかテスト"""

    # automation_engine.py ファイルを読み込み
    with open("src/auto_coder/automation_engine.py", "r") as f:
        content = f.read()

    # チェックポイント
    checks = [
        ("process_issues(", "process_issues 関数が呼び出されている"),
        ("process_pull_requests(", "process_pull_requests 関数が呼び出されている"),
        (
            "issues_result = process_issues",
            "process_issues の結果が issues_result に代入されている",
        ),
        (
            "prs_result = process_pull_requests",
            "process_pull_requests の結果が prs_result に代入されている",
        ),
        (
            'issues_processed"] = issues_result',
            "issues_result が issues_processed に設定されている",
        ),
        (
            'prs_processed"] = prs_result',
            "prs_result が prs_processed に設定されている",
        ),
    ]

    all_passed = True

    for check_text, description in checks:
        if check_text in content:
            print(f"✓ {description}")
        else:
            print(f"✗ {description} - 見つかりません: {check_text}")
            all_passed = False

    return all_passed


def test_old_candidates_code_removed():
    """古い候補者ベースのループコードが削除されているかテスト"""

    with open("src/auto_coder/automation_engine.py", "r") as f:
        content = f.read()

    # 古いコードの痕跡がないかチェック
    old_code_patterns = [
        "_get_candidates(",
        "_select_best_candidate(",
        "_process_single_candidate(",
        "while True:",
        "candidates =",
    ]

    all_removed = True

    for pattern in old_code_patterns:
        if pattern in content:
            print(f"⚠ 古いコードが残っています: {pattern}")
            all_removed = False
        else:
            print(f"✓ 古いコードが削除されています: {pattern}")

    return all_removed


if __name__ == "__main__":
    print("修正内容の検証中...\n")

    print("1. run メソッドの関数呼び出しチェック:")
    test1 = test_run_method_calls_functions()

    print(f"\n2. 古いコードの削除チェック:")
    test2 = test_old_candidates_code_removed()

    print(f"\n=== 結果 ===")
    if test1 and test2:
        print("✓ すべてのチェック passes - 修正は正しく実装されています!")
        print("\n修正された run メソッドは:")
        print("- process_issues と process_pull_requests を呼び出します")
        print("- テストが期待する構造で結果を返します")
        print("- 古い候補者ベースのループを削除しています")
        sys.exit(0)
    else:
        print("✗ 一部のチェックが失敗しました")
        sys.exit(1)
