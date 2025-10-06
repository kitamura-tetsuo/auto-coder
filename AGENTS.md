# Auto-Coder Agent Guidelines

## プロジェクト概要
このプロジェクトは、AI CLIバックエンド（デフォルト: codex、--backendでgemini/qwenに切替可）を使用してアプリケーション開発を自動化するPythonアプリケーションです。
GitHubからissueやエラーのPRを取得して構築・修正を行い、必要に応じて機能追加issueを自動作成します。

## 開発ガイドライン

### コード品質
- 全ての機能には対応するテストを作成する
- テストの期待値は厳密に設定する
- テストが失敗した場合は、まずテストの期待値が仕様に一致しているかを確認する
- テストをスキップするコードは禁止
- e2eテストではモックを使用しない
- e2eテストはheadlessで実行する

### プロジェクト構造
- 標準的なPythonプロジェクト構造を維持する
- 機能は全てdocs/client-features.yamlに記載する
- 重複する関数を複数箇所に作成しない

### GitHub操作
- GitHub操作にはghコマンドを使用する
- GitHub APIを適切に使用してissueとPRを取得する

### 依存関係管理
- パッケージ管理にはpipまたはpoetryを使用する
- requirements.txtまたはpyproject.tomlを適切に管理する

### ログ設定
- loguruライブラリを使用してログ出力を行う
- ログにはファイル名、関数名、行番号を含める
- コンソール出力は色付きで見やすく表示する
- ファイル出力はローテーション機能付きで保存する


### CI/PR チェック
- GitHub Actions によるPRチェックを必須化する。
- ワークフロー: .github/workflows/ci.yml（name: CI）
- 必須ジョブ:
  - Lint & Type Check（black/isort/flake8/mypy）
  - Tests (pytest)（Python 3.11/3.12 マトリクス）
- ブランチ保護で required status checks に以下のチェック名を登録する想定：
  - "CI / Lint & Type Check"
  - "CI / Tests (pytest) (3.11)"
  - "CI / Tests (pytest) (3.12)"

### LLM実行ポリシー（重要）

## 仕様メモ（運用上の重要点）
- PR処理: PRのチェックが失敗している場合、デフォルトで PRのベースブランチ 取り込みをスキップして修正に進む（`--skip-main-update`）。従来挙動に戻すには `--no-skip-main-update` を指定。


- 分析フェーズ禁止: analyze_issue 等、分析だけを目的とした LLM 呼び出しを行わない
- 単回実行: 各 issue/PR につき LLM の実行は1回とし、その1回で修正の特定・実装・テスト・コミット/PR更新までを完結させる
- 分割実行禁止: 同一の LLM に対して複数回に分けてタスクを投げない（精度は向上しないため）
- 例外: Git/GitHub API 呼び出し、ビルド/テスト/静的解析などの非LLM処理は必要に応じて実行可。モデルの自動切替は同一回内でのみ許容
- 実装注意: CodexClient 等のクライアントに analyze_issue などのメソッドを追加・使用しない。既存コードに存在する場合は呼び出しを削除し、単回実行フローに統一する


- PR出力ポリシー: PRに対するLLMの出力はコメント投稿を禁止し、最小限のコード修正・git add/commit/push・条件を満たす場合は gh pr merge を行う。コメントやレビュー文面の出力は不可。成功時は `ACTION_SUMMARY:` で始まる1行のみを出力し、修正不能時は `CANNOT_FIX` を出力する。


- TEST_SCRIPT_PATH（scripts/test.sh）の所在と方針
  - 実行時に使用される scripts/test.sh は「対象リポジトリ」のもの。本リポジトリ内の scripts/test.sh を最適化しても効果はない。
  - 自動実行系（run_local_tests, run_pr_tests 等）は pytest 直叩きを行わず、常に TEST_SCRIPT_PATH を呼び出す。単一テスト再実行時も `bash $TEST_SCRIPT_PATH <file>` で渡す。
  - TEST_SCRIPT_PATH の存在確認は「起動時に一度だけ」行い、不在なら即エラーで終了する。それ以降の処理では TEST_SCRIPT_PATH の不在チェックを行わない（フォールバック禁止）。

### Git commit/push policy (English)

- Centralize all git commit and push operations through dedicated helper routines.
- Do not invoke `git commit` or `git push` directly in multiple places across the codebase.
- Rationale: scattering commit/push logic leads to duplicated behavior, inconsistent error handling, and subtle bugs (e.g., missing unified handling for formatter hooks like dprint).
- Implementation:
  - `git_utils.git_commit_with_retry(commit_message, cwd=None, max_retries=1)`: Centralized commit helper that automatically detects dprint formatting errors, runs `npx dprint fmt`, stages changes, and retries commit once.
  - `git_utils.git_push(cwd=None, remote='origin', branch=None)`: Centralized push helper for consistent error handling.
  - All git commit/push operations throughout the codebase use these helpers.
  - Direct invocations of `git commit` or `git push` via CommandExecutor are prohibited outside of these helpers.

### MCP-PDB セットアップ支援
- CLI `auto-coder mcp-pdb` グループを追加
  - `print-config --target [windsurf|claude]` で設定スニペットを出力
  - `status` で前提コマンド（uv）の存在チェックとセットアップ手順のヒントを表示
- 実環境へのインストールは行わず、ユーザの開発環境（Windsurf/Claude）での設定支援のみを行う



## 主要機能
- GitHub APIを使用したissue/PR取得（古い順でソート）
- **Jules Mode（オプション）**: issueに'jules'ラベルを追加、PRは通常通りAIバックエンドで処理（デフォルトは codex）
- **通常モード（デフォルト）**: デフォルトの codex または --backend 指定の Gemini / Qwen を使用した単回実行の自動処理（分析のみの呼び出しは禁止）
- **自動モデル切り替え**: PRコンフリクト時にgemini-2.5-flashに自動切り替えで高速解決
- **Package-lock.jsonコンフリクト特別処理**: package-lock.json、yarn.lock、pnpm-lock.yamlのコンフリクトを自動削除・再生成で解決
- **package.json 依存関係のみのコンフリクト自動解消**: package.jsonの非依存セクションが一致し、依存セクションのみの差分である場合に、より新しいバージョン／より多い方を優先して自動マージ
- **Geminiプロンプトエスケープ**: プロンプト内の@文字を\@に自動エスケープしてGemini CLIに安全に渡す
- 必要な機能の自動検出と issue作成
- 自動化されたコード修正と構築
- PR処理の優先順位付け（GitHub Actionsパス且つマージ可能→マージ、その他→修正）

- LLMスキップ用フラグ導入: package-lock.json等の自動解消やマージ解決後にpush完了した場合、フラグで後続のLLM分析を明示的にスキップ
- Jules ModeはデフォルトON: CLIの --jules-mode/--no-jules-mode で切替（既定はON）

- Codex-MCPモード: 単一PR処理または単回のローカルエラー修正フロー中は、`codex mcp` の永続セッションを維持。最小のJSON-RPC（initialize/echoツール呼び出し）を実装済み。高度な操作は引き続き `codex exec` で対応

## テスト戦略
- ユニットテスト: 各モジュールの個別機能をテスト
- 統合テスト: API統合とCLI統合をテスト
- e2eテスト: エンドツーエンドの自動化フローをテスト
