# MCP Inspector アクセスガイド

## 🌐 アクセスURL

### 0.0.0.0（推奨 - 外部からアクセス可能）
```
http://0.0.0.0:6274/?MCP_PROXY_AUTH_TOKEN=973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474
```

### localhost（ローカルアクセスのみ）
```
http://localhost:6274/?MCP_PROXY_AUTH_TOKEN=973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474
```

## ✅ TCPプロキシによる0.0.0.0サポート

TCPプロキシ（`tcp_proxy.py`）を使用して、以下のポートが0.0.0.0でlistenしています：

- **Inspector Web UI**: 0.0.0.0:6274
- **MCPプロキシサーバー**: 0.0.0.0:6277

### プロキシの状態確認
```bash
ps aux | grep tcp_proxy
lsof -i :6274
lsof -i :6277
```

### プロキシのテスト
```bash
# ローカルアクセス（直接）
curl -s -I http://localhost:6274 | head -3

# 外部アクセス（0.0.0.0経由）
curl -s -I http://0.0.0.0:6274 | head -3
```

## 🔍 動作確認

### 1. Webブラウザでの確認
ブラウザで上記のURLにアクセスして、MCP InspectorのUIが表示されることを確認してください。

### 2. MCPサーバーの確認
Inspector内で「test-watcher」サーバーが接続されていることを確認してください。以下の情報が表示されるはずです：
- サーバー名: test-watcher
- 利用可能なツール: 4つ
- 利用可能なリソース: 2つ

### 3. ツールのテスト
Inspectorから直接ツールを呼び出してテストできます：
- `get_status()` - テスト監視サービスの状態を確認
- `start_watching()` - ファイル監視を開始

## 🔧 トラブルシューティング

### プロキシが動作していない場合
```bash
# プロキシを再起動
pkill -f tcp_proxy.py
python3 /home/node/src/auto-coder/tcp_proxy.py 6274 localhost 6274 &
python3 /home/node/src/auto-coder/tcp_proxy.py 6277 localhost 6277 &
```

### 接続できない場合
1. プロキシプロセスが動作しているか確認
2. ポートが他のプロセスで使用されていないか確認
3. 防火墙の設定を確認

## 📊 現在の状況

- **Inspector**: 動作中
- **TCPプロキシ**: 動作中
- **test-watcherサーバー**: 設定済み
- **アクセス**: 0.0.0.0で可能

---

**更新日時**: 2025-10-31
**認証トークン**: 973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474