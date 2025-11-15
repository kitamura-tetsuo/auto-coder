#!/bin/bash
set -e
cd /workspaces/auto-coder
export PATH="$PWD/.venv/bin:$PATH"
OUTPUT=$(auto-coder --help)
echo "$OUTPUT"
# Check for expected help content
if ! echo "$OUTPUT" | grep -q "Usage:"; then
  echo "Error: Help output does not contain 'Usage:'"
  exit 1
fi
if ! echo "$OUTPUT" | grep -q "Auto-Coder"; then
  echo "Error: Help output does not contain 'Auto-Coder'"
  exit 1
fi
echo "CLI entry point test passed!"