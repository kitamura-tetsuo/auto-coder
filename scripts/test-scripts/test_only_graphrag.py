#!/usr/bin/env python3
"""
GraphRAG Individual Test
"""

import subprocess
import sys
import time


def test_only_graphrag():
    """Test only the GraphRAG server"""
    print("=== GraphRAG Server Test ===")

    try:
        # Run only the GraphRAG server
        process = subprocess.Popen([sys.executable, "src/auto_coder/mcp_servers/graphrag_mcp/server.py"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        print(f"✓ Process started (PID: {process.pid})")

        # Wait 5 seconds to see output
        try:
            time.sleep(5)
            process.terminate()
            stdout, stderr = process.communicate(timeout=2)
            print("✓ Process terminated")

            if stdout:
                print("=== STDOUT ===")
                print(stdout)

            if stderr:
                print("=== STDERR ===")
                print(stderr)

            return True

        except subprocess.TimeoutExpired:
            process.kill()
            print("✗ Timeout (server hung up)")
            return False

    except Exception as e:
        print(f"✗ Error: {e}")
        return False


if __name__ == "__main__":
    test_only_graphrag()
