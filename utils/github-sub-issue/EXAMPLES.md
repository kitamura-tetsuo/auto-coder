# 使用例

## 基本的なワークフロー

### 1. 親 issue を作成

```bash
# GitHub CLI で親 issue を作成
gh issue create --title "機能: ユーザー認証システム" --body "完全な認証システムを実装する"
# Created issue #100
```

### 2. sub-issue を作成

```bash
# データベーススキーマの設計
github-sub-issue create --parent 100 --title "データベーススキーマの設計" --label "database"

# JWT トークンの実装
github-sub-issue create --parent 100 --title "JWT トークンの実装" --label "backend"

# ログイン UI の作成
github-sub-issue create --parent 100 --title "ログイン UI の作成" --label "frontend"
```

### 3. 既存の issue を sub-issue として追加

```bash
# 既存の issue #95 を sub-issue として追加
github-sub-issue add 100 95
```

### 4. 進捗を確認

```bash
# すべての sub-issue を表示
github-sub-issue list 100 --state all

# 出力例:
# 📋 Sub-issues (4 total):
# ─────────────────────────────
# ✅ #101  データベーススキーマの設計           [closed]
# ✅ #95   セキュリティ監査チェックリスト         [closed]
# 🔵 #102  JWT トークンの実装                   [open]   @alice
# 🔵 #103  ログイン UI の作成                   [open]   @bob
```

### 5. 不要な sub-issue を削除

```bash
# sub-issue #95 を削除
github-sub-issue remove 100 95

# 複数の sub-issue を削除
github-sub-issue remove 100 95 96 97 --force
```

## 高度な使用例

### クロスリポジトリの sub-issue

```bash
# 別のリポジトリの issue を sub-issue として追加
github-sub-issue add https://github.com/owner/repo1/issues/100 \
  https://github.com/owner/repo2/issues/200
```

### JSON 出力を使った自動化

```bash
# JSON 形式で sub-issue を取得
github-sub-issue list 100 --json | jq '.[] | select(.state == "OPEN") | .number'

# 出力例:
# 102
# 103
```

### スクリプトでの使用

```bash
#!/bin/bash

# 親 issue を作成
PARENT=$(gh issue create --title "Sprint 1" --body "Sprint 1 のタスク" | grep -oP '\d+$')

# タスクリストから sub-issue を作成
while IFS= read -r task; do
  github-sub-issue create --parent "$PARENT" --title "$task" --label "sprint-1"
done < tasks.txt

# 進捗を表示
github-sub-issue list "$PARENT"
```

### CI/CD での使用

```yaml
# .github/workflows/create-sub-issues.yml
name: Create Sub-issues

on:
  issues:
    types: [labeled]

jobs:
  create-sub-issues:
    if: github.event.label.name == 'epic'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install github-sub-issue
        run: |
          cd utils/github-sub-issue
          pip install -e .
      
      - name: Create sub-issues
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          ISSUE_NUMBER=${{ github.event.issue.number }}
          
          # タスクリストから sub-issue を作成
          github-sub-issue create --parent "$ISSUE_NUMBER" \
            --title "タスク 1: 設計" --label "design"
          
          github-sub-issue create --parent "$ISSUE_NUMBER" \
            --title "タスク 2: 実装" --label "implementation"
          
          github-sub-issue create --parent "$ISSUE_NUMBER" \
            --title "タスク 3: テスト" --label "testing"
```

## トラブルシューティング

### エラー: "Failed to get current repository"

現在のディレクトリが GitHub リポジトリではない場合、`--repo` オプションを使用してください:

```bash
github-sub-issue list 123 --repo owner/repo
```

### エラー: "The provided sub-issue does not exist"

issue ID が正しく取得されていない可能性があります。`--verbose` オプションでデバッグ情報を確認してください:

```bash
github-sub-issue --verbose add 123 456
```

### エラー: "authentication required"

GitHub CLI が認証されていることを確認してください:

```bash
gh auth status
gh auth login
```

## ベストプラクティス

### 1. 適切な粒度で sub-issue を作成

- 大きすぎる sub-issue は避ける (1-3日で完了できるサイズが理想)
- 小さすぎる sub-issue も避ける (チェックリストで十分な場合もある)

### 2. ラベルを活用

```bash
github-sub-issue create --parent 100 \
  --title "API エンドポイントの実装" \
  --label "backend,api,priority-high"
```

### 3. アサインを明確に

```bash
github-sub-issue create --parent 100 \
  --title "フロントエンド実装" \
  --assignee "@me"
```

### 4. 定期的に進捗を確認

```bash
# 毎日の進捗確認
github-sub-issue list 100 --state all

# JSON 形式で進捗率計算
github-sub-issue list 100 --json | \
  jq '[.[] | select(.state == "CLOSED")] | length' | \
  awk '{print "完了率: " ($1/4)*100 "%"}'
```

