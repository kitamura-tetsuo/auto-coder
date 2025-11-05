#!/usr/bin/env python3
"""
Test Watcher MCP Communication Test (Echo Server Compliant Version)
FastMCP 2024-11-05 Protocol Compliant
"""

import json
import subprocess
import sys
import time

def _read_headers(stdin):
    """Read headers in compliance with FastMCP echo server"""
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
    """Send message in compliance with FastMCP echo server"""
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stdin.write(f"Content-Length: {len(b)}\r\n\r\n".encode("ascii"))
    stdin.write(b)
    stdin.flush()

def test_mcp_communication():
    """MCP protocol communication test (Echo Server compliant version)"""
    print("=== Test Watcher MCP Communication Test (Echo Server Compliant Version) ===")

    try:
        # Start process (no buffering, standard I/O)
        process = subprocess.Popen(
            [sys.executable, "src/auto_coder/mcp_servers/test_watcher/main.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0  # No buffering
        )

        print(f"✓ Process started (PID: {process.pid})")

        # 1. Send initialization message
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
        print("✓ initialize sent")

        # Receive initialization response
        headers = _read_headers(process.stdout)
        if headers is None:
            print("✗ Failed to receive initialization header")
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
                            print(f"✓ initialize successful: {server_info.get('name', 'Unknown')}")
                        else:
                            print(f"✗ initialize failed: {response}")
                    except Exception as e:
                        print(f"✗ Initialization JSON parsing error: {e}")
                else:
                    print("✗ Failed to receive initialization body")
            else:
                print("✗ Content-Length missing")

        # 2. tools/list request
        tools_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }

        _send_message(process.stdin, tools_request)
        print("✓ tools/list sent")

        # Receive tools/list response
        tools_headers = _read_headers(process.stdout)
        if tools_headers is None:
            print("✗ Failed to receive tools/list header")
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
                            print(f"✓ tools/list successful: {len(tools)} tools")
                            for tool in tools:
                                print(f"  - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')}")
                        else:
                            print(f"✗ tools/list failed: {tools_response}")
                    except Exception as e:
                        print(f"✗ tools/list JSON parsing error: {e}")
                else:
                    print("✗ Failed to receive tools/list body")

        # 3. ping request
        ping_request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "ping"
        }

        _send_message(process.stdin, ping_request)
        print("✓ ping sent")

        # Receive ping response
        ping_headers = _read_headers(process.stdout)
        if ping_headers is None:
            print("✗ Failed to receive ping header")
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
                            print(f"✓ ping successful: {ping_response['result']}")
                        else:
                            print(f"✗ ping failed: {ping_response}")
                    except Exception as e:
                        print(f"✗ ping JSON parsing error: {e}")
                else:
                    print("✗ Failed to receive ping body")

        # 4. get_status tool call test
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
        print("✓ get_status tool sent")

        # Receive get_status response
        status_headers = _read_headers(process.stdout)
        if status_headers is None:
            print("✗ Failed to receive get_status header")
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
                            print(f"✓ get_status successful")
                            print(f"  - File watcher running: {status_data.get('file_watcher_running', False)}")
                            print(f"  - Project root: {status_data.get('project_root', 'Unknown')}")
                        else:
                            print(f"✗ get_status failed: {status_response}")
                    except Exception as e:
                        print(f"✗ get_status JSON parsing error: {e}")
                else:
                    print("✗ Failed to receive get_status body")

        # Cleanup
        process.stdin.close()
        process.wait(timeout=5)
        print("✓ Process terminated normally")

        return True

    except Exception as e:
        print(f"✗ Error: {e}")
        if 'process' in locals():
            try:
                process.kill()
            except Exception:
                pass
        return False

if __name__ == "__main__":
    test_mcp_communication()
