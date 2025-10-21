# Graph Builder

TypeScript と Python のコードベースを解析して Neo4j グラフデータベース用のデータを生成するツールです。

## 概要

このツールは、TypeScript と Python のプロジェクトを解析し、以下を抽出します:

- **ノード**: File, Module, Function, Method, Class, Interface, Type
- **エッジ**: IMPORTS, CALLS, CONTAINS, EXTENDS, IMPLEMENTS

各ノードには以下の情報が含まれます:

- `id`: 一意識別子（SHA1ハッシュの16文字）
- `kind`: ノードの種類
- `fqname`: 完全修飾名（例: `src/user/service.ts:UserService.getUserById`）
- `sig`: 型シグネチャ（例: `(string)->Promise<User>`）
- `short`: 要約（30-80トークン、JSDocまたは自動生成）
- `complexity`: 循環的複雑度
- `tokens_est`: 推定トークン数
- `tags`: 副作用タグ（IO, DB, NETWORK, ASYNC, PURE）

## インストール

### TypeScript版

```bash
cd graph-builder
npm install
npm run build
```

### Python版

```bash
cd graph-builder
# 依存関係は標準ライブラリのみ
```

## 使い方

### TypeScript版

```bash
# プロジェクトをスキャン
node dist/cli.js scan --project ./my-project --out ./out --languages typescript,python

# CSV出力（Neo4j bulk import用）
node dist/cli.js emit-csv --out ./out

# JSON出力（バッチ操作用）
node dist/cli.js emit-json --out ./out

# Git差分から変更を検出
node dist/cli.js diff --since HEAD~1 --out ./out
```

### Python版

```bash
# プロジェクトをスキャン
python3 src/cli_python.py scan --project ./my-project --out ./out

# CSV出力（Neo4j bulk import用）
python3 src/cli_python.py emit-csv --out ./out

# JSON出力（バッチ操作用）
python3 src/cli_python.py emit-json --out ./out

# Git差分から変更を検出
python3 src/cli_python.py diff --since HEAD~1 --out ./out
```

## コマンドオプション

### scan

プロジェクトをスキャンしてグラフデータを抽出します。

```bash
graph-builder scan [options]
```

**オプション:**

- `--project <path>`: プロジェクトのパス（デフォルト: `.`）
- `--out <path>`: 出力ディレクトリ（デフォルト: `./out`）
- `--mode <mode>`: スキャンモード `full` または `diff`（デフォルト: `full`）
- `--since <ref>`: diff モード用の Git リファレンス
- `--limit <number>`: 処理するファイル数の制限
- `--batch-size <number>`: 出力のバッチサイズ（デフォルト: `500`）
- `--languages <langs>`: スキャンする言語（カンマ区切り: `typescript,python`）

### emit-csv

Neo4j bulk import 用の CSV ファイルを出力します。

```bash
graph-builder emit-csv [options]
```

**オプション:**

- `--out <path>`: 出力ディレクトリ（デフォルト: `./out`）

**出力ファイル:**

- `nodes.csv`: ノードデータ
- `rels.csv`: エッジデータ

### emit-json

バッチ操作用の JSON ファイルを出力します。

```bash
graph-builder emit-json [options]
```

**オプション:**

- `--out <path>`: 出力ディレクトリ（デフォルト: `./out`）

**出力ファイル:**

- `batch-{timestamp}.json`: バッチデータ

### diff

Git の変更から差分を生成します。

```bash
graph-builder diff [options]
```

**オプション:**

- `--since <ref>`: 比較する Git リファレンス（デフォルト: `HEAD~1`）
- `--out <path>`: 出力ディレクトリ（デフォルト: `./out`）

**出力ファイル:**

- `diff-{commit}.json`: 差分データ

## 出力スキーマ

### nodes.csv

```csv
id:ID,kind,fqname,sig,short,complexity:int,tokens_est:int,tags,file,start_line:int,end_line:int
a1b2c3d4e5f6,Function,src/user/service.ts:UserService.getUserById,(string)->Promise<User>,"gets user by id",6,28,DB;IO,src/user/service.ts,42,58
```

### rels.csv

```csv
:START_ID,:END_ID,type,count:int,locations
a1b2c3d4e5f6,aabbcc112233,CALLS,1,"[{""file"":""src/user/service.ts"",""line"":42}]"
```

### batch JSON

```json
{
  "nodes": [
    {
      "id": "a1b2c3",
      "kind": "Function",
      "fqname": "src/x:Foo.bar",
      "sig": "(id:string)->Promise<User>",
      "short": "fetch user by id",
      "complexity": 5,
      "tokens_est": 25,
      "tags": ["DB", "IO"]
    }
  ],
  "edges": [
    {
      "from": "a1b2c3",
      "to": "d4e5f6",
      "type": "CALLS",
      "count": 1
    }
  ]
}
```

### diff JSON

```json
{
  "meta": {
    "commit": "abcdef",
    "files": ["src/x.ts"],
    "timestamp": "2025-10-21T12:00:00Z"
  },
  "added": {
    "nodes": [...],
    "edges": [...]
  },
  "updated": {
    "nodes": [...],
    "edges": [...]
  },
  "removed": {
    "nodes": ["..."],
    "edges": ["..."]
  }
}
```

## Neo4j へのインポート

### Bulk Import（初回ロード）

```bash
# CSVファイルを生成
graph-builder scan --project ./my-project --out ./out
graph-builder emit-csv --out ./out

# Neo4j にインポート
neo4j-admin import \
  --nodes=./out/nodes.csv \
  --relationships=./out/rels.csv \
  --delimiter=',' \
  --array-delimiter=';'
```

### オンライン/差分更新（UNWIND）

```bash
# JSONバッチファイルを生成
graph-builder scan --project ./my-project --out ./out
graph-builder emit-json --out ./out

# Cypherクエリで読み込み
CALL apoc.load.json('file:///out/batch-*.json') YIELD value
UNWIND value.nodes AS node
MERGE (n {id: node.id})
SET n = node

UNWIND value.edges AS edge
MATCH (from {id: edge.from})
MATCH (to {id: edge.to})
MERGE (from)-[r:CALLS]->(to)
SET r.count = edge.count
```

## CI/CD 連携

### GitHub Actions の例

```yaml
name: Update Code Graph

on:
  push:
    branches: [main]

jobs:
  update-graph:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Setup Node.js
        uses: actions/setup-node@v3
        with:
          node-version: '18'
      
      - name: Install graph-builder
        run: |
          cd graph-builder
          npm install
          npm run build
      
      - name: Generate diff
        run: |
          node graph-builder/dist/cli.js diff --since ${{ github.event.before }} --out ./artifacts
      
      - name: Upload artifacts
        uses: actions/upload-artifact@v3
        with:
          name: graph-diff
          path: ./artifacts/diff-*.json
```

## テスト

### TypeScript

```bash
npm test
```

### Python

```bash
python3 src/tests/test_python_scanner.py
```

## 既知の制限

- **シンボル未解決**: 外部ライブラリや動的インポートは `unresolved: true` タグが付きます
- **型推論**: TypeScript の複雑な型推論は完全にはサポートされていません
- **Python の型ヒント**: 型ヒントがない場合は `Any` として扱われます

## ライセンス

MIT

## 貢献

プルリクエストを歓迎します！

## サポート

問題が発生した場合は、GitHub Issues で報告してください。

