# gh-sub-issue

GitHub の sub-issues 機能を操作するための Python ユーティリティーです。

## 特徴

- ✅ **正しい GraphQL API を使用**: GitHub の公式 sub-issues API を使用
- 🔗 **既存 issue を sub-issue として追加**: 既存の issue を親 issue に紐付け
- ➕ **新しい sub-issue を作成**: 新規 issue を作成して親に紐付け
- 📋 **sub-issue の一覧表示**: 親 issue の sub-issue を一覧表示
- ❌ **sub-issue の削除**: 親 issue から sub-issue を削除
- 🎨 **複数の出力形式**: TTY (色付き)、プレーンテキスト、JSON 出力をサポート

## 前提条件

- Python 3.11 以上
- GitHub CLI (`gh`) がインストールされ、認証済みであること

## インストール

```bash
# リポジトリのルートから
cd utils/gh-sub-issue
pip install -e .
```

## 使い方

### 既存 issue を sub-issue として追加

```bash
# issue 番号を使用 (親 issue 123 に既存 issue 456 を追加)
gh-sub-issue add 123 456

# URL を使用
gh-sub-issue add https://github.com/owner/repo/issues/123 456

# リポジトリを指定
gh-sub-issue add 123 456 --repo owner/repo
```

### 新しい sub-issue を作成

```bash
# 基本的な使い方
gh-sub-issue create --parent 123 --title "ユーザー認証の実装"

# 説明とラベルを追加
gh-sub-issue create --parent 123 \
  --title "ログインエンドポイントの追加" \
  --body "POST /api/login エンドポイントを実装" \
  --label "backend,api" \
  --assignee "@me"

# 親 issue の URL を使用
gh-sub-issue create \
  --parent https://github.com/owner/repo/issues/123 \
  --title "API テストを書く"
```

### sub-issue の一覧表示

```bash
# 基本的な一覧表示
gh-sub-issue list 123

# すべての状態を表示 (open, closed)
gh-sub-issue list 123 --state all

# JSON 出力
gh-sub-issue list 123 --json

# URL を使用
gh-sub-issue list https://github.com/owner/repo/issues/123
```

### sub-issue の削除

```bash
# 単一の sub-issue を削除
gh-sub-issue remove 123 456

# 複数の sub-issue を削除
gh-sub-issue remove 123 456 457 458

# 確認をスキップ
gh-sub-issue remove 123 456 --force

# URL を使用
gh-sub-issue remove https://github.com/owner/repo/issues/123 456
```

## 開発

### テストの実行

```bash
# すべてのテストを実行
pytest

# カバレッジ付きで実行
pytest --cov=gh_sub_issue --cov-report=html
```

### コードフォーマット

```bash
# フォーマット
black .
isort .

# リント
flake8
mypy .
```

## ライセンス

MIT License

## 技術的な詳細

### GraphQL API の使用

このツールは GitHub の GraphQL API を使用して sub-issues を操作します。重要なポイント:

1. **GraphQL-Features ヘッダーが必要**: すべての sub-issues 関連の API 呼び出しには `GraphQL-Features: sub_issues` ヘッダーが必要です
2. **Issue ID を使用**: issue 番号ではなく、issue ID (例: `I_kwDOOakzpM6yyU6H`) を使用する必要があります
3. **Mutations を使用**: sub-issue の追加・削除には GraphQL mutations を使用します

### yahsan2/gh-sub-issue との違い

[yahsan2/gh-sub-issue](https://github.com/yahsan2/gh-sub-issue) は Go で実装された同様のツールですが、
このツールは以下の点で異なります:

- **Python 実装**: auto-coder プロジェクトとの統合が容易
- **本体から独立**: auto-coder の依存関係を持たない独立したユーティリティー
- **正しい GraphQL API を使用**: GitHub の公式 sub-issues API を使用し、GraphQL で正しく認識される sub-issue を作成

## 参考

- [GitHub Sub-issues Public Preview](https://github.com/orgs/community/discussions/148714)
- [GitHub GraphQL API - Sub-issues](https://docs.github.com/en/graphql/reference/mutations#addsubissue)
- [Create GitHub issue hierarchy using the API](https://jessehouwing.net/create-github-issue-hierarchy-using-the-api/)
- [yahsan2/gh-sub-issue](https://github.com/yahsan2/gh-sub-issue)

