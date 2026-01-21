#!/usr/bin/env python3
"""
Test client for checking MCP server operation
"""

import json
import subprocess
import sys
import time
from typing import Any, Dict


def test_mcp_server_with_initialize(server_script: str) -> Dict[str, Any]:
    """Test MCP server by initializing and getting tool list"""

    # MCP initialization message
    init_message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test-client", "version": "1.0.0"}}}

    # tools/list request message
    tools_request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

    # Start server process
    try:
        process = subprocess.Popen([sys.executable, server_script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Send initialization message
        init_json = json.dumps(init_message) + "\n"
        tools_json = json.dumps(tools_request) + "\n"

        process.stdin.write(init_json)
        process.stdin.write(tools_json)
        process.stdin.flush()

        # Read responses
        stdout, stderr = process.communicate(timeout=10)

        # Parse responses
        lines = stdout.strip().split("\n")
        responses = []
        for line in lines:
            if line.strip():
                try:
                    response = json.loads(line)
                    responses.append(response)
                except json.JSONDecodeError:
                    pass

        return {"success": True, "responses": responses, "stdout": stdout, "stderr": stderr}

    except subprocess.TimeoutExpired:
        process.kill()
        return {"success": False, "error": "Timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    # Servers to test
    servers = ["src/auto_coder/mcp_servers/test_watcher/server.py", "src/auto_coder/mcp_servers/graphrag_mcp/server.py"]

    for server in servers:
        print(f"\n=== Testing: {server} ===")
        result = test_mcp_server_with_initialize(server)

        if result["success"]:
            print("✓ Server started successfully")

            # Parse responses
            for response in result["responses"]:
                if "result" in response and "tools" in response["result"]:
                    tools = response["result"]["tools"]
                    print(f"Available tools count: {len(tools)}")
                    for tool in tools:
                        print(f"  - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')}")
                elif "error" in response:
                    print(f"Error: {response['error']}")
        else:
            print(f"✗ Server startup failed: {result.get('error', 'Unknown error')}")


if __name__ == "__main__":
    main()
