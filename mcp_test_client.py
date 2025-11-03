#!/usr/bin/env python3
"""
MCPサーバー動作確認用のテストクライアント
"""

import json
import subprocess
import sys
import time
from typing import Any, Dict


def test_mcp_server_with_initialize(server_script: str) -> Dict[str, Any]:
    """MCPサーバーを初期化してツール一覧を取得するテスト"""

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

    # サーバープロセスを開始
    try:
        process = subprocess.Popen(
            [sys.executable, server_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # 初期化メッセージを送信
        init_json = json.dumps(init_message) + "\n"
        tools_json = json.dumps(tools_request) + "\n"

        process.stdin.write(init_json)
        process.stdin.write(tools_json)
        process.stdin.flush()

        # 応答を読み取り
        stdout, stderr = process.communicate(timeout=10)

        # 応答を解析
        lines = stdout.strip().split("\n")
        responses = []
        for line in lines:
            if line.strip():
                try:
                    response = json.loads(line)
                    responses.append(response)
                except json.JSONDecodeError:
                    pass

        return {
            "success": True,
            "responses": responses,
            "stdout": stdout,
            "stderr": stderr,
        }

    except subprocess.TimeoutExpired:
        process.kill()
        return {"success": False, "error": "Timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    # テスト対象サーバー
    servers = [
        "src/auto_coder/mcp_servers/test_watcher/server.py",
        "src/auto_coder/mcp_servers/graphrag_mcp/server.py",
    ]

    for server in servers:
        print(f"\n=== テスト中: {server} ===")
        result = test_mcp_server_with_initialize(server)

        if result["success"]:
            print("✓ サーバー起動成功")

            # 応答解析
            for response in result["responses"]:
                if "result" in response and "tools" in response["result"]:
                    tools = response["result"]["tools"]
                    print(f"利用可能なツール数: {len(tools)}")
                    for tool in tools:
                        print(
                            f"  - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')}"
                        )
                elif "error" in response:
                    print(f"エラー: {response['error']}")
        else:
            print(f"✗ サーバー起動失敗: {result.get('error', 'Unknown error')}")


if __name__ == "__main__":
    main()
