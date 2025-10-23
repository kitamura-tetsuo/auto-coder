# バンドルされたMCPサーバー

## 概要

auto-coder v2025.10.23以降、カスタマイズされたGraphRAG MCPサーバーがパッケージにバンドルされています。

## 変更内容

### 以前（v2025.10.23より前）

- `auto-coder graphrag setup-mcp`コマンドは`https://github.com/rileylemm/graphrag_mcp`をクローン
- 汎用的なドキュメント検索用MCPサーバーを使用
- ツール: `search_documentation`, `hybrid_search`

### 現在（v2025.10.23以降）

- `auto-coder graphrag setup-mcp`コマンドはバンドルされたカスタムMCPサーバーをコピー
- TypeScript/JavaScriptコード分析専用MCPサーバーを使用
- ツール: `find_symbol`, `get_call_graph`, `get_dependencies`, `impact_analysis`, `semantic_code_search`

## バンドルされたMCPサーバーの場所

### パッケージ内

```
src/auto_coder/mcp_servers/graphrag_mcp/
├── FORK_INFO.md              # フォーク情報
├── server.py                 # MCPサーバー本体
├── main.py                   # エントリーポイント
├── pyproject.toml            # 依存関係定義
├── run_server.sh             # 起動スクリプト
└── graphrag_mcp/
    ├── code_analysis_tool.py # コード分析ツール実装
    └── documentation_tool.py # 元のツール（未使用）
```

### インストール後

```bash
# pipx経由でインストールした場合
pipx install auto-coder

# setup-mcpコマンドでMCPサーバーをコピー
auto-coder graphrag setup-mcp

# デフォルトのコピー先
~/graphrag_mcp/
```

## 使用方法

### 1. auto-coderのインストール

```bash
# pipx経由（推奨）
pipx install auto-coder

# または pip経由
pip install auto-coder
```

### 2. GraphRAGサービスの起動

```bash
# Neo4j + Qdrantを起動
auto-coder graphrag start
```

### 3. MCPサーバーのセットアップ

#### 自動セットアップ（推奨）

**v2025.10.23以降、MCPサーバーは自動的にセットアップされます。**

auto-coderを実行すると、`~/graphrag_mcp`ディレクトリが存在しない場合、自動的にセットアップが実行されます。

```bash
# 通常のコマンドを実行するだけで自動セットアップされます
auto-coder run

# または任意のコマンド
auto-coder process-issues
```

#### 手動セットアップ

手動でセットアップしたい場合は、以下のコマンドを実行します：

```bash
# バンドルされたMCPサーバーを~/graphrag_mcpにコピー
auto-coder graphrag setup-mcp

# カスタムディレクトリにコピー
auto-coder graphrag setup-mcp --install-dir /path/to/custom/dir
```

このコマンドは以下を実行します：
1. バンドルされたMCPサーバーを指定ディレクトリにコピー
2. `uv`を使用して依存関係をインストール
3. `.env`ファイルを作成（Neo4j/Qdrant接続情報）
4. 各バックエンド（Codex, Gemini, Qwen, Windsurf/Claude）の設定ファイルを自動更新

### 4. MCPサーバーの起動確認

```bash
# MCPサーバーを手動起動（テスト用）
cd ~/graphrag_mcp
uv run main.py

# または起動スクリプトを使用
./run_server.sh
```

## カスタマイズ内容

### フォーク元

- オリジナル: https://github.com/rileylemm/graphrag_mcp
- 用途: 汎用的なドキュメント検索

### カスタマイズ

1. **グラフスキーマの変更**
   - 元: Document, Chunk, Category ノード
   - 現在: File, Symbol（Function, Method, Class, Interface, Type）ノード

2. **リレーションシップの変更**
   - 元: PART_OF, RELATED_TO, HAS_CATEGORY
   - 現在: CONTAINS, CALLS, EXTENDS, IMPLEMENTS, IMPORTS

3. **ツールの追加**
   - `find_symbol(fqname)`: シンボル検索
   - `get_call_graph(symbol_id, direction, depth)`: 呼び出しグラフ分析
   - `get_dependencies(file_path)`: 依存関係分析
   - `impact_analysis(symbol_ids, max_depth)`: 影響範囲分析
   - `semantic_code_search(query, limit, kind_filter)`: 意味的コード検索

4. **自己記述の強化**
   - ツールdocstringにts-morph固有の情報を追加
   - MCPリソースにコード分析ドメイン知識を追加

## 技術的な詳細

### パッケージング

- `pyproject.toml`の`[tool.setuptools.package-data]`にMCPサーバーを追加
- `MANIFEST.in`でMCPサーバーファイルを明示的に含める
- pipx/pipインストール時に自動的にバンドル

### セットアップフロー

```python
# src/auto_coder/cli_commands_graphrag.py

def run_graphrag_setup_mcp_programmatically(...):
    # 1. パッケージ内のMCPサーバーを検索
    import auto_coder
    package_dir = Path(auto_coder.__file__).parent
    bundled_mcp = package_dir / "mcp_servers" / "graphrag_mcp"
    
    # 2. インストール先にコピー
    shutil.copytree(bundled_mcp, install_path)
    
    # 3. 依存関係をインストール
    subprocess.run(["uv", "sync"], cwd=install_path)
    
    # 4. .envファイルを作成
    # 5. バックエンド設定を更新
```

### ts-morphとの統合

MCPサーバーは`src/auto_coder/graph_builder/`のts-morphスキャナーが生成するグラフ構造に対応：

```typescript
// src/auto_coder/graph_builder/src/types.ts

export type NodeKind = 'File' | 'Function' | 'Method' | 'Class' | 'Interface' | 'Type';
export type EdgeType = 'IMPORTS' | 'CALLS' | 'CONTAINS' | 'EXTENDS' | 'IMPLEMENTS';

export interface CodeNode {
  id: string;
  kind: NodeKind;
  fqname: string;
  sig: string;
  complexity: number;
  file: string;
  start_line: number;
  end_line: number;
  tags?: string[];
}
```

## トラブルシューティング

### MCPサーバーが見つからない

```bash
# パッケージが正しくインストールされているか確認
python -c "import auto_coder; from pathlib import Path; print(Path(auto_coder.__file__).parent / 'mcp_servers' / 'graphrag_mcp')"

# 再インストール
pipx reinstall auto-coder
```

### setup-mcpが失敗する

```bash
# uvがインストールされているか確認
uv --version

# uvをインストール
curl -LsSf https://astral.sh/uv/install.sh | sh

# 既存のディレクトリを削除して再実行
rm -rf ~/graphrag_mcp
auto-coder graphrag setup-mcp
```

### MCPサーバーが起動しない

```bash
# 依存関係を再インストール
cd ~/graphrag_mcp
uv sync

# Neo4j/Qdrantが起動しているか確認
auto-coder graphrag status

# .envファイルを確認
cat ~/graphrag_mcp/.env
```

## 参照

- フォーク情報: `src/auto_coder/mcp_servers/graphrag_mcp/FORK_INFO.md`
- 機能ドキュメント: `docs/client-features.yaml` (external_dependencies.graphrag_mcp)
- セットアップコマンド: `src/auto_coder/cli_commands_graphrag.py`
- テスト: `tests/test_graphrag_mcp_fork.py`

## 今後の拡張

1. **他言語対応**: Python, Go, Rustのサポート追加
2. **高度なクエリ**: アーキテクチャ分析、デッドコード検出
3. **増分更新**: 大規模コードベースの最適化
4. **キャッシング**: 頻繁なクエリのキャッシュ
5. **可視化**: コールグラフの可視化ツール

