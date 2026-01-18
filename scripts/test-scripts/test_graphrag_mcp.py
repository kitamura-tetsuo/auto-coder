#!/usr/bin/env python3
"""
GraphRAG MCP Server Test Client
"""

import json
import sys
import subprocess
import time
from typing import Dict, Any

def test_graphrag_mcp():
    """Test GraphRAG MCP server"""

    # MCP initialization message
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

    # find_symbol test
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
        print("Starting GraphRAG MCP server...")
        process = subprocess.Popen(
            [sys.executable, "src/auto_coder/mcp_servers/graphrag_mcp/server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        responses = []

        # initialize
        print("Sending: initialize")
        process.stdin.write(json.dumps(init_message) + "\n")
        process.stdin.flush()
        time.sleep(2)

        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"Received: initialize -> {response.get('result', {}).get('serverInfo', {}).get('name', 'Unknown')}")
            except json.JSONDecodeError:
                print(f"Failed to parse initialization response JSON: {line[:100]}")

        # ping
        print("Sending: ping")
        process.stdin.write(json.dumps(ping_request) + "\n")
        process.stdin.flush()
        time.sleep(1)

        line = process.stdout.readline()
        if line:
            try:
                response = json.loads(line)
                responses.append(response)
                print(f"Received: ping -> {response.get('result', 'No result')}")
            except json.JSONDecodeError:
                print(f"Failed to parse ping response JSON: {line[:100]}")

        # tools/list
        print("Sending: tools/list")
        process.stdin.write(json.dumps(tools_request) + "\n")
        process.stdin.flush()
        time.sleep(2)

        # Read remaining responses
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
                            print(f"Received: tools/list -> {len(tools)} tools")
                            for tool in tools:
                                print(f"  - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')[:100]}")
                        else:
                            print(f"Received: {response}")
                    except json.JSONDecodeError as e:
                        print(f"JSON parsing failed: {line[:100]} - {e}")

        # find_symbol test
        print("Sending: find_symbol test")
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
                        print(f"Received: find_symbol -> Success")
                        print(f"  -fqname: {symbol.get('fqname')}")
                        print(f"  kind: {symbol.get('kind')}")
                        print(f"  file: {symbol.get('file')}")
                    else:
                        print(f"Received: find_symbol -> Failed: {response['result'].get('error', 'Unknown error')}")
                else:
                    print(f"Received: find_symbol -> Error: {response.get('error', 'Unknown error')}")
            except json.JSONDecodeError:
                print(f"Failed to parse find_symbol response JSON: {line[:100]}")

        # Server termination
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
    print("=== GraphRAG MCP Server Operation Check ===")
    result = test_graphrag_mcp()

    if result["success"]:
        print("\n✅ GraphRAG MCP Server Test Success")
    else:
        print(f"\n❌ GraphRAG MCP Server Test Failed: {result.get('error', 'Unknown error')}")
