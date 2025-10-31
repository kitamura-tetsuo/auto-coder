#!/usr/bin/env python3
"""
Test Watcher MCP通信テスト (Echo Server準拠版)
FastMCP 2024-11-05 プロトコル準拠
"""

import json
import subprocess
import sys
import time

def _read_headers(stdin):
    """FastMCP echo server準拠ヘッダー読み取り"""
    data = b""
    while True:
        line = stdin.readline()
        if not line:
            return None
        data += line
        if data.endswith(b"\r\n\r\n"):
            break
    headers = {}
    for h in data.decode("utf-8", errors="ignore").split("\r\n"):
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers

def _send_message(stdin, obj):
    """FastMCP echo server準拠メッセージ送信"""
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stdin.write(f"Content-Length: {len(b)}\r\n\r\n".encode("ascii"))
    stdin.write(b)
    stdin.flush()

def test_mcp_communication():
    """MCPプロトコル通信テスト (Echo Server準拠版)"""
    print("=== Test Watcher MCP通信テスト (Echo Server準拠版) ===")
    
    try:
        # プロセス開始 (バッファリングなし、標準I/O)
        process = subprocess.Popen(
            [sys.executable, "src/auto_coder/mcp_servers/test_watcher/main.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0  # バッファリングなし
        )
        
        print(f"✓ プロセス開始 (PID: {process.pid})")
        
        # 1. 初期化メッセージ送信
        init_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0.0"
                }
            }
        }
        
        _send_message(process.stdin, init_message)
        print("✓ initialize送信")
        
        # 初期化応答受信
        headers = _read_headers(process.stdout)
        if headers is None:
            print("✗ 初期化ヘッダー受信失敗")
        else:
            try:
                length = int(headers.get("content-length", "0"))
            except Exception:
                length = 0
            
            if length > 0:
                body = process.stdout.read(length)
                if body:
                    try:
                        response = json.loads(body.decode("utf-8"))
                        if "result" in response:
                            server_info = response["result"].get("serverInfo", {})
                            print(f"✓ initialize成功: {server_info.get('name', 'Unknown')}")
                        else:
                            print(f"✗ initialize失敗: {response}")
                    except Exception as e:
                        print(f"✗ 初期化JSON解析エラー: {e}")
                else:
                    print("✗ 初期化ボディ受信失敗")
            else:
                print("✗ Content-Length missing")
        
        # 2. tools/list リクエスト
        tools_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }
        
        _send_message(process.stdin, tools_request)
        print("✓ tools/list送信")
        
        # tools/list 応答受信
        tools_headers = _read_headers(process.stdout)
        if tools_headers is None:
            print("✗ tools/listヘッダー受信失敗")
        else:
            try:
                tools_length = int(tools_headers.get("content-length", "0"))
            except Exception:
                tools_length = 0
            
            if tools_length > 0:
                tools_body = process.stdout.read(tools_length)
                if tools_body:
                    try:
                        tools_response = json.loads(tools_body.decode("utf-8"))
                        if "result" in tools_response and "tools" in tools_response["result"]:
                            tools = tools_response["result"]["tools"]
                            print(f"✓ tools/list成功: {len(tools)} 個のツール")
                            for tool in tools:
                                print(f"  - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')}")
                        else:
                            print(f"✗ tools/list失敗: {tools_response}")
                    except Exception as e:
                        print(f"✗ tools/list JSON解析エラー: {e}")
                else:
                    print("✗ tools/listボディ受信失敗")
        
        # 3. ping リクエスト
        ping_request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "ping"
        }
        
        _send_message(process.stdin, ping_request)
        print("✓ ping送信")
        
        # ping 応答受信
        ping_headers = _read_headers(process.stdout)
        if ping_headers is None:
            print("✗ pingヘッダー受信失敗")
        else:
            try:
                ping_length = int(ping_headers.get("content-length", "0"))
            except Exception:
                ping_length = 0
            
            if ping_length > 0:
                ping_body = process.stdout.read(ping_length)
                if ping_body:
                    try:
                        ping_response = json.loads(ping_body.decode("utf-8"))
                        if "result" in ping_response:
                            print(f"✓ ping成功: {ping_response['result']}")
                        else:
                            print(f"✗ ping失敗: {ping_response}")
                    except Exception as e:
                        print(f"✗ ping JSON解析エラー: {e}")
                else:
                    print("✗ pingボディ受信失敗")
        
        # 4. get_status ツール呼び出しテスト
        get_status_request = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "get_status",
                "arguments": {}
            }
        }
        
        _send_message(process.stdin, get_status_request)
        print("✓ get_statusツール送信")
        
        # get_status 応答受信
        status_headers = _read_headers(process.stdout)
        if status_headers is None:
            print("✗ get_statusヘッダー受信失敗")
        else:
            try:
                status_length = int(status_headers.get("content-length", "0"))
            except Exception:
                status_length = 0
            
            if status_length > 0:
                status_body = process.stdout.read(status_length)
                if status_body:
                    try:
                        status_response = json.loads(status_body.decode("utf-8"))
                        if "result" in status_response:
                            status_data = status_response["result"]
                            print(f"✓ get_status成功")
                            print(f"  - ファイルウォッチャー: {status_data.get('file_watcher_running', False)}")
                            print(f"  - プロジェクトルート: {status_data.get('project_root', 'Unknown')}")
                        else:
                            print(f"✗ get_status失敗: {status_response}")
                    except Exception as e:
                        print(f"✗ get_status JSON解析エラー: {e}")
                else:
                    print("✗ get_statusボディ受信失敗")
        
        # クリーンアップ
        process.stdin.close()
        process.wait(timeout=5)
        print("✓ プロセス正常終了")
        
        return True
        
    except Exception as e:
        print(f"✗ エラー: {e}")
        if 'process' in locals():
            try:
                process.kill()
            except Exception:
                pass
        return False

if __name__ == "__main__":
    test_mcp_communication()