#!/bin/bash
set -e

# This reproduces the GitHub Actions test
echo "Reproducing GitHub Actions test..."

# Add .venv/bin to PATH to access installed console scripts
export PATH="$PWD/.venv/bin:$PATH"

OUTPUT=$(auto-coder --help)
echo "Help output:"
echo "$OUTPUT"
echo ""

echo "Testing for 'Usage:'..."
if echo "$OUTPUT" | grep -q "Usage:"; then
    echo "✓ Found 'Usage:' in output"
else
    echo "✗ Missing 'Usage:' in output"
    exit 1
fi

echo "Testing for 'Auto-Coder'..."
if echo "$OUTPUT" | grep -q "Auto-Coder"; then
    echo "✓ Found 'Auto-Coder' in output"
else
    echo "✗ Missing 'Auto-Coder' in output"
    exit 1
fi

echo "All tests passed!"