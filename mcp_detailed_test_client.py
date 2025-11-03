#!/usr/bin/env python3
"""
MCPサーバー動作確認用の詳細テストクライアント
"""

import json
import subprocess
import sys
import time
from typing import Any, Dict, List


def test_mcp_server_detailed(server_script: str) -> Dict[str, Any]:
    """MCPサーバーを詳細にテストする"""

    # MCP初期化メッセージ
    init_message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }

    # tools/list リクエストメッセージ
    tools_request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

    # ping リクエスト
    ping_request = {"jsonrpc": "2.0", "id": 3, "method": "ping"}

    try:
        process = subprocess.Popen(
            [sys.executable, server_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0,
        )

        responses = []

        # 初期化メッセージ送信
        print("送信: initialize")
        init_json = json.dumps(init_message) + "\n"
        process.stdin.write(init_json)
        process.stdin.flush()
        time.sleep(0.5)

        # 初期化応答を待つ
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"受信: {response}")
            except json.JSONDecodeError:
                print(f"初期化応答のJSON解析に失敗: {line}")

        # ping送信
        print("送信: ping")
        ping_json = json.dumps(ping_request) + "\n"
        process.stdin.write(ping_json)
        process.stdin.flush()
        time.sleep(0.5)

        # ping応答を待つ
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"受信: {response}")
            except json.JSONDecodeError:
                print(f"ping応答のJSON解析に失敗: {line}")

        # tools/list送信
        print("送信: tools/list")
        tools_json = json.dumps(tools_request) + "\n"
        process.stdin.write(tools_json)
        process.stdin.flush()
        time.sleep(1.0)

        # 全ての応答を読み取り
        remaining_output = process.stdout.read()
        if remaining_output:
            lines = remaining_output.strip().split("\n")
            for line in lines:
                if line.strip():
                    try:
                        response = json.loads(line)
                        responses.append(response)
                        print(f"受信: {response}")
                    except json.JSONDecodeError:
                        print(f"JSON解析に失敗: {line}")

        # プロセス終了
        process.stdin.close()
        process.wait(timeout=5)

        return {"success": True, "responses": responses}

    except subprocess.TimeoutExpired:
        process.kill()
        return {"success": False, "error": "Timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    print("=== MCPサーバー詳細テスト ===")

    # Test Watcherサーバー詳細テスト
    print("\n1. Test Watcherサーバー詳細テスト:")
    result = test_mcp_server_detailed(
        "src/auto_coder/mcp_servers/test_watcher/server.py"
    )

    if result["success"]:
        print("✓ サーバー起動成功")

        # 応答を解析
        for response in result["responses"]:
            if "result" in response:
                if "tools" in response["result"]:
                    tools = response["result"]["tools"]
                    print(f"利用可能なツール数: {len(tools)}")
                    for tool in tools:
                        print(f"  - ツール名: {tool.get('name', 'Unknown')}")
                        print(f"    説明: {tool.get('description', 'No description')}")
                        if "inputSchema" in tool:
                            print(
                                f"    パラメーター: {tool['inputSchema'].get('properties', {}).keys()}"
                            )
                        print()
                elif response["result"] == {}:
                    print("  ping 応答受信")
            elif "error" in response:
                print(f"エラー: {response['error']}")
    else:
        print(f"✗ サーバー起動失敗: {result.get('error', 'Unknown error')}")


if __name__ == "__main__":
    main()
