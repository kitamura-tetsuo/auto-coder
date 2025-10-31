# MCP Inspector Configuration

このディレクトリには、MCP InspectorでMCPサーバーを視覚化するための設定ファイルが含まれています。

## セットアップ手順

### 1. MCP Inspectorの起動

次のコマンドでMCP Inspectorを起動します：

```bash
mcp-inspector /home/node/src/auto-coder/mcp-inspector-config/mcp-servers.json
```

### 2. Webブラウザでアクセス

Inspectorはデフォルトで http://localhost:5173 で起動します。ブラウザでアクセスしてください。

## 接続済みMCPサーバー

### 1. graphrag-mcp
- **説明**: GraphRAG コード解析サーバー
- **機能**:
  - コード閾の検索
  - コールグラフ分析
  - 依存関係分析
  - インパクト分析
  -  семанティックコード検索
- **依存関係**: neo4j, qdrant-client, sentence-transformers

### 2. test-watcher
- **説明**: テスト監視サーバー（ファイル変更の監視と自動テスト実行）
- **機能**:
  - ファイル監視の開始/停止
  - テスト結果のクエリ
  - ステータス取得
- **依存関係**: loguru, watchdog, pathspec

## 環境要件

### graphrag-mcp
- Neo4j データベースが起動している必要
- Qdrant ベクトルデータベースが起動している必要
- コードグラフが構築済みであること

### test-watcher
- Node.js および npm がインストール済み
- Playwright がインストール済み: `npm install -D @playwright/test`
- プロジェクトルートは `/home/node/src/auto-coder`

## トラブルシューティング

### サーバーが起動しない場合
1. 必要な依存関係がインストールされていることを確認
2. 環境変数が正しく設定されていることを確認
3. ログを確認してエラーの詳細を調べる

### データベース接続エラー（graphrag-mcp）
Neo4j または Qdrant が起動していない可能性があります：
```bash
# Neo4j の起動（例）
neo4j start

# Qdrant の起動（例）
qdrant
```