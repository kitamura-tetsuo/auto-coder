#!/usr/bin/env python3
"""
GraphRAG MCP サーバーテストクライアント
"""

import json
import sys
import subprocess
import time
from typing import Dict, Any

def test_graphrag_mcp():
    """GraphRAG MCPサーバーをテスト"""
    
    # MCP初期化メッセージ
    init_message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "graphrag-test-client",
                "version": "1.0.0"
            }
        }
    }
    
    # ping
    ping_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "ping"
    }
    
    # tools/list
    tools_request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/list"
    }
    
    # find_symbol テスト
    find_symbol_request = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "find_symbol",
            "arguments": {
                "fqname": "src/auto_coder/mcp_servers/graphrag_mcp/server.py::main"
            }
        }
    }
    
    try:
        print("GraphRAG MCPサーバーを起動中...")
        process = subprocess.Popen(
            [sys.executable, "src/auto_coder/mcp_servers/graphrag_mcp/server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        responses = []
        
        # initialize
        print("送信: initialize")
        process.stdin.write(json.dumps(init_message) + "\n")
        process.stdin.flush()
        time.sleep(2)
        
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"受信: initialize -> {response.get('result', {}).get('serverInfo', {}).get('name', 'Unknown')}")
            except json.JSONDecodeError:
                print(f"初期化応答のJSON解析に失敗: {line[:100]}")
        
        # ping
        print("送信: ping")
        process.stdin.write(json.dumps(ping_request) + "\n")
        process.stdin.flush()
        time.sleep(1)
        
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"受信: ping -> {response.get('result', 'No result')}")
            except json.JSONDecodeError:
                print(f"ping応答のJSON解析に失敗: {line[:100]}")
        
        # tools/list
        print("送信: tools/list")
        process.stdin.write(json.dumps(tools_request) + "\n")
        process.stdin.flush()
        time.sleep(2)
        
        # 残りの応答を読み取り
        remaining_output = process.stdout.read()
        if remaining_output:
            lines = remaining_output.strip().split('\n')
            for line in lines:
                if line.strip():
                    try:
                        response = json.loads(line)
                        responses.append(response)
                        if 'tools' in response.get('result', {}):
                            tools = response['result']['tools']
                            print(f"受信: tools/list -> {len(tools)} 個のツール")
                            for tool in tools:
                                print(f"  - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')[:100]}")
                        else:
                            print(f"受信: {response}")
                    except json.JSONDecodeError as e:
                        print(f"JSON解析に失敗: {line[:100]} - {e}")
        
        # find_symbolテスト
        print("送信: find_symbol テスト")
        process.stdin.write(json.dumps(find_symbol_request) + "\n")
        process.stdin.flush()
        time.sleep(3)
        
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                if 'result' in response:
                    symbol = response['result'].get('symbol')
                    if symbol:
                        print(f"受信: find_symbol -> 成功")
                        print(f"  -find: {symbol.get('fqname')}")
                        print(f"  kind: {symbol.get('kind')}")
                        print(f"  file: {symbol.get('file')}")
                    else:
                        print(f"受信: find_symbol -> 失敗: {response['result'].get('error', 'Unknown error')}")
                else:
                    print(f"受信: find_symbol -> エラー: {response.get('error', 'Unknown error')}")
            except json.JSONDecodeError:
                print(f"find_symbol応答のJSON解析に失敗: {line[:100]}")
        
        # サーバー終了
        process.stdin.close()
        process.wait(timeout=5)
        
        return {
            "success": True,
            "responses": responses
        }
        
    except subprocess.TimeoutExpired:
        process.kill()
        return {"success": False, "error": "Timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    print("=== GraphRAG MCPサーバー動作確認 ===")
    result = test_graphrag_mcp()
    
    if result["success"]:
        print("\n✅ GraphRAG MCPサーバー テスト成功")
    else:
        print(f"\n❌ GraphRAG MCPサーバー テスト失敗: {result.get('error', 'Unknown error')}")