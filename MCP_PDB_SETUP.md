# MCP-PDB セットアップ完了

## 概要
MCP-PDB（Model Context Protocol Python Debugger）ツールがこのワークスペースで利用可能になりました。
このツールはPythonデバッガー（pdb）をMCP経由でClaude等のLLMから利用できるようにします。

## セットアップ状況

### ✅ 完了した作業
1. **Python 3.13のインストール**: MCP-PDBはPython 3.13以上が必要
2. **MCP-PDBツールのインストール**: uvを使用してインストール完了
3. **サーバーの起動**: MCP-PDBサーバーが正常に起動中
4. **動作確認**: テストファイルでPythonコードの実行を確認

### 🔧 現在の状態
- **MCP-PDBサーバー**: 実行中（Terminal ID: 57）
- **Python実行環境**: Python 3.13.6
- **作業ディレクトリ**: `/home/ubuntu/src/auto-coder`
- **テストファイル**: `test_debug_sample.py` 作成済み

## 利用方法

### 1. Windsurf での設定
settings.json に以下を追加：

```json
{
  "mcpServers": {
    "mcp-pdb": {
      "command": "uv",
      "args": [
        "run",
        "--python",
        "3.13",
        "--with",
        "mcp-pdb",
        "mcp-pdb"
      ]
    }
  }
}
```

### 2. Claude Code での設定
以下のコマンドを実行：

```bash
claude mcp add mcp-pdb -- uv run --python 3.13 --with mcp-pdb mcp-pdb
```

### 3. 利用可能なツール

| ツール | 説明 |
|--------|------|
| `start_debug(file_path, use_pytest, args)` | Pythonファイルのデバッグセッションを開始 |
| `send_pdb_command(command)` | 実行中のPDBインスタンスにコマンドを送信 |
| `set_breakpoint(file_path, line_number)` | 特定の行にブレークポイントを設定 |
| `clear_breakpoint(file_path, line_number)` | 特定の行のブレークポイントをクリア |
| `list_breakpoints()` | 現在のブレークポイントを一覧表示 |
| `restart_debug()` | 現在のデバッグセッションを再開 |
| `examine_variable(variable_name)` | 変数の詳細情報を取得 |
| `get_debug_status()` | デバッグセッションの現在の状態を表示 |
| `end_debug()` | 現在のデバッグセッションを終了 |

### 4. よく使用するPDBコマンド

| コマンド | 説明 |
|----------|------|
| `n` | 次の行（ステップオーバー） |
| `s` | 関数内にステップイン |
| `c` | 実行を継続 |
| `r` | 現在の関数から戻る |
| `p variable` | 変数の値を表示 |
| `pp variable` | 変数を整形して表示 |
| `l` | ソースコードを表示 |
| `q` | デバッグを終了 |

## テスト用ファイル

`test_debug_sample.py` が作成されており、以下の機能をテストできます：
- 階乗計算関数
- フィボナッチ数列計算関数
- エラーハンドリング

## 注意事項

⚠️ **セキュリティ警告**: このツールはPythonコードをデバッガー経由で実行します。信頼できる環境でのみ使用してください。

## サーバー管理

### サーバーの状態確認
```bash
# プロセス一覧を確認
ps aux | grep mcp-pdb
```

### サーバーの停止（必要な場合）
現在実行中のMCP-PDBサーバーを停止する場合は、Terminal ID 57のプロセスを終了してください。

### サーバーの再起動
```bash
uv run --python 3.13 --with mcp-pdb mcp-pdb
```

## 次のステップ

1. **IDE設定**: WindsurfまたはClaude Codeで上記の設定を追加
2. **接続確認**: MCPサーバーとの接続を確認
3. **デバッグ開始**: `start_debug()` ツールを使用してデバッグセッションを開始

MCP-PDBツールが正常にセットアップされ、Pythonコードのデバッグが可能になりました。
