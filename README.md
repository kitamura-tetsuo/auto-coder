# Auto-Coder

AI CLIバックエンド（デフォルト: codex、--backendでgeminiに切替可）を用いてアプリケーション開発を自動化するPythonアプリケーションです。GitHubからissueやエラーのPRを取得して構築・修正を行い、必要に応じて機能追加issueを自動作成します。

## 機能

### 🔧 主要機能
- **GitHub API統合**: issueとPRの自動取得・管理
- **AI分析（codexデフォルト／Gemini切替可）**: issueとPRの内容を自動分析
- **自動化処理**: 分析結果に基づく自動アクション
- **機能提案**: リポジトリ分析による新機能の自動提案
- **レポート生成**: 処理結果の詳細レポート

### 🚀 自動化ワークフロー
1. **Issue処理**: オープンなissueを取得し、Gemini AIで分析
2. **PR処理**: オープンなPRを取得し、リスクレベルを評価
3. **機能提案**: リポジトリコンテキストから新機能を提案
4. **自動アクション**: 分析結果に基づくコメント追加や自動クローズ

## インストール

### 前提条件
- Python 3.9以上
- [gh CLI](https://cli.github.com/) で事前に認証済みであること（`gh auth login`）
- [Codex CLI](https://github.com/openai/codex) がインストール済み（デフォルトのバックエンド）
- [Gemini CLI](https://ai.google.dev/gemini-api/docs/cli?hl=ja) は Gemini バックエンドを使用する場合に必要（`gemini login`）

### セットアップ

1. リポジトリをクローン:
```bash
git clone https://github.com/your-username/auto-coder.git
cd auto-coder
```

2. 依存関係をインストールして、任意のディレクトリから実行可能にします:
```bash
source ./venv/bin/activate
pip install -e .
# またはリポジトリをクローンせずに直接インストール
pip install git+https://github.com/your-username/auto-coder.git
```

3. 必要に応じて設定ファイルを作成:
```bash
cp .env.example .env
# トークンはgh・geminiの認証情報が自動的に使用されるため空欄でも動作します
```

## 使用方法

### 認証

基本的には `gh auth login` を実施してください。Gemini バックエンドを使用する場合は `gemini login` を行うことで、APIキーを環境変数に設定せずに利用できます（codex バックエンドでは --model は無視されます）。

### CLIコマンド

#### issueとPRの処理
```bash
# デフォルト（codex バックエンド）で実行
auto-coder process-issues --repo owner/repo

# バックエンドを gemini に切替してモデル指定
auto-coder process-issues --repo owner/repo --backend gemini --model gemini-2.5-pro

# ドライランモードで実行（変更を行わない）
auto-coder process-issues --repo owner/repo --dry-run

# 特定のIssue/PRのみ処理（番号指定）
auto-coder process-issues --repo owner/repo --only 123

# 特定のPRのみ処理（URL指定）
auto-coder process-issues --repo owner/repo --only https://github.com/owner/repo/pull/456
```

#### 機能提案issueの作成
```bash
# デフォルト（codex バックエンド）で実行
auto-coder create-feature-issues --repo owner/repo

# バックエンドを gemini に切替してモデル指定
auto-coder create-feature-issues --repo owner/repo --backend gemini --model gemini-2.5-pro
```

### コマンドオプション

#### `process-issues`
- `--repo`: GitHubリポジトリ (owner/repo形式)
- `--backend`: 使用するAIバックエンド（codex|gemini）。デフォルトは codex。
- `--model`: モデル指定（Geminiのみ有効。backend=codex の場合は無視され、警告が表示されます）
- `--dry-run`: ドライランモード（変更を行わない）
- `--skip-main-update/--no-skip-main-update`: PRのチェックが失敗している場合に、修正を試みる前に main ブランチをPRブランチへ取り込むかの挙動を切替（デフォルト: main取り込みをスキップ）。
  - 既定値: `--skip-main-update`（スキップ）
  - 明示的に main 取り込みを行いたい場合は `--no-skip-main-update` を指定
- `--only`: 特定のIssue/PRのみ処理（URLまたは番号指定）

オプション:
- `--github-token`: gh CLIの認証情報を使用しない場合に手動指定
- `--gemini-api-key`: Gemini バックエンド使用時に、CLI認証情報を使わない場合の手動指定

#### `create-feature-issues`
- `--repo`: GitHubリポジトリ (owner/repo形式)
- `--backend`: 使用するAIバックエンド（codex|gemini）。デフォルトは codex。
- `--model`: モデル指定（Geminiのみ有効。backend=codex の場合は無視され、警告が表示されます）

オプション:
- `--github-token`: gh CLIの認証情報を使用しない場合に手動指定
- `--gemini-api-key`: Gemini バックエンド使用時に、CLI認証情報を使わない場合の手動指定

## 設定

### 環境変数

| 変数名 | 説明 | デフォルト値 | 必須 |
|--------|------|-------------|------|
| `GITHUB_TOKEN` | GitHub APIトークン (gh CLIの認証情報を上書きする場合) | - | ❌ |
| `GEMINI_API_KEY` | Gemini APIキー (Gemini CLIの認証情報を上書きする場合) | - | ❌ |
| `GITHUB_API_URL` | GitHub API URL | `https://api.github.com` | ❌ |
| `GEMINI_MODEL` | 使用するGeminiモデル | `gemini-pro` | ❌ |
| `MAX_ISSUES_PER_RUN` | 1回の実行で処理する最大issue数 | `-1` | ❌ |
| `MAX_PRS_PER_RUN` | 1回の実行で処理する最大PR数 | `-1` | ❌ |
| `DRY_RUN` | ドライランモード | `false` | ❌ |
| `LOG_LEVEL` | ログレベル | `INFO` | ❌ |

`MAX_ISSUES_PER_RUN` と `MAX_PRS_PER_RUN` はデフォルトで制限なし (`-1`) に設定されています。処理件数を制限したい場合は、正の整数を指定してください。

## 開発

### 開発環境のセットアップ

1. 開発用依存関係をインストール:
```bash
pip install -e ".[dev]"
```

2. pre-commitフックをセットアップ:
```bash
pre-commit install
```

### VS Code デバッグ設定

プロジェクトには以下のデバッグ設定が含まれています：

- **Auto-Coder: Process Issues (Dry Run)**: outlinerディレクトリでドライランモード実行
- **Auto-Coder: Create Feature Issues**: outlinerディレクトリで機能提案issue作成
- **Auto-Coder: Auth Status**: outlinerディレクトリで認証状況確認
- **Auto-Coder: Process Issues (Live)**: outlinerディレクトリで実際の処理実行

デバッグを開始するには：
1. VS Codeで `F5` を押すか、「実行とデバッグ」パネルを開く
2. 上記の設定から選択して実行
3. ブレークポイントを設定してステップ実行が可能

### テストの実行

```bash
# 全テストを実行
pytest

# カバレッジ付きでテストを実行
pytest --cov=src/auto_coder --cov-report=html

# 特定のテストファイルを実行
pytest tests/test_github_client.py
```

### コード品質チェック

```bash
# フォーマット
black src/ tests/

# インポート順序
isort src/ tests/

# リンター
flake8 src/ tests/

# 型チェック
mypy src/
```

## アーキテクチャ

### コンポーネント構成

```
src/auto_coder/
├── cli.py              # CLIエントリーポイント
├── github_client.py    # GitHub API クライアント
├── gemini_client.py    # Gemini AI クライアント
├── automation_engine.py # メイン自動化エンジン
└── config.py          # 設定管理
```

### データフロー

1. **CLI** → **AutomationEngine** → **GitHubClient** (データ取得)
2. **AutomationEngine** → **GeminiClient** (AI分析)
3. **AutomationEngine** → **GitHubClient** (アクション実行)
4. **AutomationEngine** → **レポート生成**

## 出力とレポート

実行結果は `reports/` ディレクトリにJSON形式で保存されます:

- `automation_report_*.json`: 自動化処理の結果
- `feature_suggestions_*.json`: 機能提案の結果

## トラブルシューティング

### よくある問題

1. **GitHub API制限**: レート制限に達した場合は時間をおいて再実行
2. **Gemini API エラー**: APIキーが正しく設定されているか確認
3. **権限エラー**: GitHubトークンに適切な権限があるか確認

### ログの確認

```bash
# ログレベルを DEBUG に設定
export LOG_LEVEL=DEBUG
auto-coder process-issues --repo owner/repo --dry-run
```

## ライセンス

このプロジェクトはMITライセンスの下で公開されています。詳細は[LICENSE](LICENSE)ファイルを参照してください。

## 貢献

プルリクエストや issue の報告を歓迎します。貢献する前に、以下を確認してください:

1. テストが通ること
2. コードスタイルが統一されていること
3. 新機能には適切なテストが含まれていること

## サポート

問題や質問がある場合は、GitHubのissueを作成してください。