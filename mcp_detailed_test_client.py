#!/usr/bin/env python3
"""
Detailed test client for checking MCP server operation
"""

import json
import sys
import subprocess
import time
from typing import Dict, Any, List

def test_mcp_server_detailed(server_script: str) -> Dict[str, Any]:
    """Test MCP server in detail"""

    # MCP initialization message
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

    # tools/list request message
    tools_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list"
    }

    # ping request
    ping_request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "ping"
    }

    try:
        process = subprocess.Popen(
            [sys.executable, server_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0
        )

        responses = []

        # Send initialization message
        print("Sending: initialize")
        init_json = json.dumps(init_message) + "\n"
        process.stdin.write(init_json)
        process.stdin.flush()
        time.sleep(0.5)

        # Wait for initialization response
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"Received: {response}")
            except json.JSONDecodeError:
                print(f"Failed to parse initialization response JSON: {line}")

        # Send ping
        print("Sending: ping")
        ping_json = json.dumps(ping_request) + "\n"
        process.stdin.write(ping_json)
        process.stdin.flush()
        time.sleep(0.5)

        # Wait for ping response
        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"Received: {response}")
            except json.JSONDecodeError:
                print(f"Failed to parse ping response JSON: {line}")

        # Send tools/list
        print("Sending: tools/list")
        tools_json = json.dumps(tools_request) + "\n"
        process.stdin.write(tools_json)
        process.stdin.flush()
        time.sleep(1.0)

        # Read all responses
        remaining_output = process.stdout.read()
        if remaining_output:
            lines = remaining_output.strip().split('\n')
            for line in lines:
                if line.strip():
                    try:
                        response = json.loads(line)
                        responses.append(response)
                        print(f"Received: {response}")
                    except json.JSONDecodeError:
                        print(f"JSON parsing failed: {line}")

        # Terminate process
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

def main():
    print("=== MCP Server Detailed Test ===")

    # Test Watcher server detailed test
    print("\n1. Test Watcher Server Detailed Test:")
    result = test_mcp_server_detailed("src/auto_coder/mcp_servers/test_watcher/server.py")

    if result["success"]:
        print("✓ Server started successfully")

        # Parse responses
        for response in result["responses"]:
            if "result" in response:
                if "tools" in response["result"]:
                    tools = response["result"]["tools"]
                    print(f"Available tools count: {len(tools)}")
                    for tool in tools:
                        print(f"  - Tool name: {tool.get('name', 'Unknown')}")
                        print(f"    Description: {tool.get('description', 'No description')}")
                        if 'inputSchema' in tool:
                            print(f"    Parameters: {tool['inputSchema'].get('properties', {}).keys()}")
                        print()
                elif response["result"] == {}:
                    print("  Ping response received")
            elif "error" in response:
                print(f"Error: {response['error']}")
    else:
        print(f"✗ Server startup failed: {result.get('error', 'Unknown error')}")

if __name__ == "__main__":
    main()