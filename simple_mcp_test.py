#!/usr/bin/env python3
"""
シンプルなMCPサーバーテストクライアント
"""

import json
import subprocess
import sys
import time


def test_simple_mcp(server_path, server_name):
    """シンプルなMCPサーバー接続テスト"""
    print(f"\n=== {server_name} テスト ===")

    try:
        # MCP初期化メッセージ
        init_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "simple-test-client", "version": "1.0.0"},
            },
        }

        # プロセス開始
        process = subprocess.Popen(
            [sys.executable, server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        print(f"✓ プロセス開始 (PID: {process.pid})")

        # 初期化メッセージ送信
        init_json = json.dumps(init_message) + "\n"
        process.stdin.write(init_json)
        process.stdin.flush()

        # 初期化応答を待機
        time.sleep(1)

        # 応答を読み取り
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line.strip())
                print(
                    f"✓ 初期化応答受信: {response.get('result', {}).get('serverInfo', {}).get('name', 'Unknown')}"
                )
            except json.JSONDecodeError as e:
                print(f"✗ JSON解析エラー: {e}")
                print(f"   受信データ: {line[:100]}")
        else:
            print("✗ 初期化応答なし")

        # tools/list リクエスト
        tools_request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

        tools_json = json.dumps(tools_request) + "\n"
        process.stdin.write(tools_json)
        process.stdin.flush()

        print("送信: tools/list")

        # tools/list 応答待機
        time.sleep(2)

        # 残りの応答を読み取り
        all_output = process.stdout.read()
        if all_output:
            lines = all_output.strip().split("\n")
            for i, line in enumerate(lines):
                if line.strip():
                    try:
                        response = json.loads(line)
                        if "tools" in response.get("result", {}):
                            tools = response["result"]["tools"]
                            print(f"✓ ツール一覧受信: {len(tools)} 個のツール")
                            for tool in tools[:5]:  # 最初の5個だけ表示
                                print(f"  - {tool.get('name', 'Unknown')}")
                            if len(tools) > 5:
                                print(f"  ... 他 {len(tools)-5} 個")
                        else:
                            print(f"応答 {i+1}: {response}")
                    except json.JSONDecodeError as e:
                        print(f"ツール応答JSON解析エラー: {e}")
                        print(f"   行 {i+1}: {line[:100]}")

        # クリーンアップ
        process.stdin.close()
        process.wait(timeout=5)
        print(f"✓ プロセス終了")

        return True

    except Exception as e:
        print(f"✗ エラー: {e}")
        if "process" in locals():
            process.kill()
        return False


def main():
    print("=== シンプルMCPサーバーテスト ===")

    servers = [
        ("src/auto_coder/mcp_servers/test_watcher/server.py", "Test Watcher"),
        ("src/auto_coder/mcp_servers/graphrag_mcp/server.py", "GraphRAG"),
    ]

    results = {}
    for server_path, server_name in servers:
        results[server_name] = test_simple_mcp(server_path, server_name)

    print(f"\n=== テスト結果 ===")
    for name, success in results.items():
        status = "✓ 成功" if success else "✗ 失敗"
        print(f"{name}: {status}")


if __name__ == "__main__":
    main()
