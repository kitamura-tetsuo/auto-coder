#!/bin/bash

# Navigate to the project directory
cd "$(dirname "$0")"

# Run the MCP server, redirecting stderr to a log file
"$(dirname "$0")/.venv/bin/python" "$(dirname "$0")/server.py" 2> server.log

