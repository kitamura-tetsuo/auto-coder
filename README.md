# Auto-Coder

Gemini CLIを使用してアプリケーション開発を自動化するPythonアプリケーションです。GitHubからissueやエラーのPRを取得して構築・修正を行い、必要に応じて機能追加issueを自動作成します。

## 機能

### 🔧 主要機能
- **GitHub API統合**: issueとPRの自動取得・管理
- **Gemini AI分析**: issueとPRの内容を自動分析
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
- GitHub API トークン
- Gemini API キー

### セットアップ

1. リポジトリをクローン:
```bash
git clone https://github.com/your-username/auto-coder.git
cd auto-coder
```

2. 依存関係をインストール:
```bash
pip install -e .
```

3. 環境変数を設定:
```bash
cp .env.example .env
# .envファイルを編集してAPIキーを設定
```

## 使用方法

### 環境変数の設定

必須の環境変数:
```bash
export GITHUB_TOKEN="your_github_token"
export GEMINI_API_KEY="your_gemini_api_key"
```

### CLIコマンド

#### issueとPRの処理
```bash
# ドライランモードで実行（変更を行わない）
auto-coder process-issues --repo owner/repo --dry-run

# 実際に処理を実行
auto-coder process-issues --repo owner/repo
```

#### 機能提案issueの作成
```bash
# リポジトリを分析して機能提案issueを作成
auto-coder create-feature-issues --repo owner/repo
```

### コマンドオプション

#### `process-issues`
- `--repo`: GitHubリポジトリ (owner/repo形式)
- `--github-token`: GitHub APIトークン (環境変数でも設定可能)
- `--gemini-api-key`: Gemini APIキー (環境変数でも設定可能)
- `--dry-run`: ドライランモード（変更を行わない）

#### `create-feature-issues`
- `--repo`: GitHubリポジトリ (owner/repo形式)
- `--github-token`: GitHub APIトークン (環境変数でも設定可能)
- `--gemini-api-key`: Gemini APIキー (環境変数でも設定可能)

## 設定

### 環境変数

| 変数名 | 説明 | デフォルト値 | 必須 |
|--------|------|-------------|------|
| `GITHUB_TOKEN` | GitHub APIトークン | - | ✅ |
| `GEMINI_API_KEY` | Gemini APIキー | - | ✅ |
| `GITHUB_API_URL` | GitHub API URL | `https://api.github.com` | ❌ |
| `GEMINI_MODEL` | 使用するGeminiモデル | `gemini-pro` | ❌ |
| `MAX_ISSUES_PER_RUN` | 1回の実行で処理する最大issue数 | `10` | ❌ |
| `MAX_PRS_PER_RUN` | 1回の実行で処理する最大PR数 | `5` | ❌ |
| `DRY_RUN` | ドライランモード | `false` | ❌ |
| `LOG_LEVEL` | ログレベル | `INFO` | ❌ |

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