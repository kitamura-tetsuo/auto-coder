#!/usr/bin/env python3
"""
Simple MCP server test client
"""

import json
import subprocess
import sys
import time

def test_simple_mcp(server_path, server_name):
    """Simple MCP server connection test"""
    print(f"\n=== {server_name} Test ===")

    try:
        # MCP initialization message
        init_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "simple-test-client",
                    "version": "1.0.0"
                }
            }
        }

        # Start process
        process = subprocess.Popen(
            [sys.executable, server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        print(f"✓ Process started (PID: {process.pid})")

        # Send initialization message
        init_json = json.dumps(init_message) + "\n"
        process.stdin.write(init_json)
        process.stdin.flush()

        # Wait for initialization response
        time.sleep(1)

        # Read response
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line.strip())
                print(f"✓ Received initialization response: {response.get('result', {}).get('serverInfo', {}).get('name', 'Unknown')}")
            except json.JSONDecodeError as e:
                print(f"✗ JSON parsing error: {e}")
                print(f"   Received data: {line[:100]}")
        else:
            print("✗ No initialization response")

        # tools/list request
        tools_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }

        tools_json = json.dumps(tools_request) + "\n"
        process.stdin.write(tools_json)
        process.stdin.flush()

        print("Sent: tools/list")

        # Wait for tools/list response
        time.sleep(2)

        # Read remaining responses
        all_output = process.stdout.read()
        if all_output:
            lines = all_output.strip().split('\n')
            for i, line in enumerate(lines):
                if line.strip():
                    try:
                        response = json.loads(line)
                        if 'tools' in response.get('result', {}):
                            tools = response['result']['tools']
                            print(f"✓ Received tool list: {len(tools)} tools")
                            for tool in tools[:5]:  # Display only first 5
                                print(f"  - {tool.get('name', 'Unknown')}")
                            if len(tools) > 5:
                                print(f"  ... and {len(tools)-5} more")
                        else:
                            print(f"Response {i+1}: {response}")
                    except json.JSONDecodeError as e:
                        print(f"Tool response JSON parsing error: {e}")
                        print(f"   Line {i+1}: {line[:100]}")

        # Cleanup
        process.stdin.close()
        process.wait(timeout=5)
        print(f"✓ Process terminated")

        return True

    except Exception as e:
        print(f"✗ Error: {e}")
        if 'process' in locals():
            process.kill()
        return False

def main():
    print("=== Simple MCP Server Test ===")

    servers = [
        ("src/auto_coder/mcp_servers/test_watcher/server.py", "Test Watcher"),
        ("src/auto_coder/mcp_servers/graphrag_mcp/server.py", "GraphRAG")
    ]

    results = {}
    for server_path, server_name in servers:
        results[server_name] = test_simple_mcp(server_path, server_name)

    print(f"\n=== Test Results ===")
    for name, success in results.items():
        status = "✓ Success" if success else "✗ Failed"
        print(f"{name}: {status}")

if __name__ == "__main__":
    main()
