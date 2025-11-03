#!/usr/bin/env python3
"""Test script to run the test-watcher MCP server"""

import os
import sys

# Set environment variable
os.environ["TEST_WATCHER_PROJECT_ROOT"] = "/home/node/src/auto-coder"
os.environ["PYO3_USE_ABI3_FORWARD_COMPATIBILITY"] = "1"

# Add paths
sys.path.insert(0, "/home/node/src/auto-coder/src")
sys.path.insert(0, "/home/node/src/auto-coder/src/auto_coder/mcp_servers/test_watcher")

# Import and run the MCP server
from auto_coder.mcp_servers.test_watcher.server import mcp

mcp.run()
