#!/bin/bash
# CI/CD用のPyright型チェックスクリプト

echo "=== Pyright型チェック実行 ==="

# 全体チェック（JSON出力）
echo "--- 全体チェック (JSON) ---"
npx pyright src/ --outputjson > pyright_results.json

# エラーと警告のみを抽出
echo "--- エラーと警告のみ表示 ---"
npx pyright src/ 2>&1 | grep -E "(error|warning)" | head -20

# 特定のファイルの詳細チェック
echo "--- automation_engine.pyの詳細 ---"
npx pyright src/auto_coder/automation_engine.py

# エラー数の統計
echo "--- エラー統計 ---"
ERROR_COUNT=$(npx pyright src/ 2>&1 | grep -c "error:")
WARNING_COUNT=$(npx pyright src/ 2>&1 | grep -c "warning:")
echo "エラー数: $ERROR_COUNT"
echo "警告数: $WARNING_COUNT"

# CI判定（エラーが0の場合のみ成功、警告は許容）
if [ $ERROR_COUNT -eq 0 ]; then
    echo "✅ 型チェック: 成功 (エラー: $ERROR_COUNT, 警告: $WARNING_COUNT)"
    exit 0
else
    echo "❌ 型チェック: 失敗 (エラー $ERROR_COUNT 個, 警告: $WARNING_COUNT 個)"
    exit 1
fi