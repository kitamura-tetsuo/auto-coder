# TCPプロキシ安定化 documentation

## 🎯 解決した問題

**問題**: 6277 порта のtcp_proxyが頻繁に停止造成 aveva problemi tecnici con la connessione

**解決方法**: 自動監視・再起動システムを構築

## 🛠️ 実装したSolution

### 1. TCP Proxy Monitor Script
- **場所**: `/home/node/src/auto-coder/monitor_proxy.sh`
- **機能**:
  - 10秒間隔でプロキシプロセスの健全性チェック
  - Stale PIDファイルの自動クリーンアップ
  - 失敗カウンターによる複数回失敗時のアラート
  - 自動再起動機能

### 2. Status Check Script
- **場所**: `/home/node/src/auto-coder/proxy_status.sh`
- **機能**:
  - 全プロキシプロセスの状態確認
  - ポートアクセス可能性テスト
  - 失敗カウンター表示
  - 監視ログの最新情報表示

### 3. 改良されたTCP Proxy
- **場所**: `/home/node/src/auto-coder/tcp_proxy.py`
- **改善点**:
  - マルチスレッド対応による同時接続処理
  - エラーハンドリングの強化

## 📊 現在の状況

### ✅ 動作中サービス
- **MCP Inspector**: localhost:6274で動作中
- **TCP Proxy (6274)**: 0.0.0.0:6274でアクセス可能
- **TCP Proxy (6277)**: 0.0.0.0:6277でアクセス可能
- **Proxy Monitor**: 10秒間隔で自動監視中

### 🔍 監視ログのサンプル
```
[2025-10-31 09:13:17] ⚠️  Proxy for port 6274 is not running (failure #1)
[2025-10-31 09:13:17] Attempting to restart proxy for port 6274...
[2025-10-31 09:13:17] Stale PID file found for port 6274, removing...
[2025-10-31 09:13:20] ✅ Proxy for port 6274 started successfully (PID: 439841)
```

## 🚀 使用方法

### 1. システム状況の確認
```bash
/home/node/src/auto-coder/proxy_status.sh
```

### 2. 手動でプロキシを再起動
```bash
# 両方のプロキシを手動で停止
pkill -f tcp_proxy.py

# 監視システムを再起動
nohup /home/node/src/auto-coder/monitor_proxy.sh > /tmp/proxy_monitor.log 2>&1 &
```

### 3. ログの確認
```bash
# 監視ログの最新情報
tail -f /tmp/proxy_monitor.log

# 各プロキシの個別ログ
tail -f /tmp/tcp_proxy_6274.log
tail -f /tmp/tcp_proxy_6277.log
```

## 📋 管理用コマンド

### プロセスの確認
```bash
# 監視プロセス
ps aux | grep monitor_proxy | grep -v grep

# TCPプロキシ
ps aux | grep tcp_proxy | grep -v grep

# PIDファイルの内容確認
cat /tmp/tcp_proxy_6274.pid
cat /tmp/tcp_proxy_6277.pid
```

### 失敗カウンターの確認
```bash
cat /tmp/tcp_proxy_6274_failures
cat /tmp/tcp_proxy_6277_failures
```

### ポート使用状況の確認
```bash
lsof -i :6274
lsof -i :6277
```

## 🔧 設定Details

### 監視間隔
- **デフォルト**: 10秒間隔
- **調整可能**: `/home/node/src/auto-coder/monitor_proxy.sh` の `sleep 10` を変更

### 再起動Threshold
- **デフォルト**: 3回の失敗でアラート
- **調整可能**: `/home/node/src/auto-coder/monitor_proxy.sh` の `RESTART_THRESHOLD` を変更

### PIDファイル管理
- **場所**: `/tmp/tcp_proxy_${port}.pid`
- **自動管理**: 監視システムが自動的に作成・更新
- **クリーンアップ**: Stale PIDファイルは自動的に削除

## ⚠️ トラブルシューティング

### プロキシが起動しない場合
1. **ログの確認**:
   ```bash
   tail -20 /tmp/proxy_monitor.log
   ```

2. **手動テスト**:
   ```bash
   python3 /home/node/src/auto-coder/tcp_proxy.py 6274 localhost 6274
   ```

3. **競合プロセスの確認**:
   ```bash
   lsof -i :6274
   ```

### 監視システムが停止した場合
1. **再起動**:
   ```bash
   pkill -f monitor_proxy.sh
   nohup /home/node/src/auto-coder/monitor_proxy.sh > /tmp/proxy_monitor.log 2>&1 &
   ```

2. **原因の調査**:
   ```bash
   tail -50 /tmp/proxy_monitor.log
   ```

## 📈 パフォーマンスStatistics

### 現在の使用状況
- **Port 6274**: MCP Inspector Web UI (0.0.0.0でアクセス可能)
- **Port 6277**: MCPプロキシサーバー (0.0.0.0でアクセス可能)
- **監視サイクル**: 10秒間隔で実行
- **平均復旧時間**: 3-5秒以内

## 🔮 将来的な改善案

1. **Prometheus メトリクス**: プロキシの性能メトリクス出力
2. **Slack/Discord アラート**: 重要な障害時の自動通知
3. **リソース制限**: CPU・メモリ使用率の監視
4. **ログローテーション**: 監視ログの自動ローテーション

## 📝 まとめ

**成果**:
✅ TCPプロキシの自動監視システムを構築
✅ 0.0.0.0 でのアクセスを実現
✅ Stale PIDファイルの自動クリーンアップ
✅ 失敗 Counter による複数失敗のアラート
✅ リアルタイムのステータス確認機能

** benefícios**:
- 手動での再起動作業が不要
- 自動復旧機能によりダウンタイムを 최소화
- リアルタイムの状態監視
- 問題の早期発見とアラート

---

**作成日**: 2025-10-31
**最終更新**: 2025-10-31 09:13