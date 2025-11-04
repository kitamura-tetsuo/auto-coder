#!/bin/bash
set -Eeuo pipefail

# -----------------------------------------------------------------------------
# Runner selection
# -----------------------------------------------------------------------------
# Prefer uv runner for consistent, reproducible environments.
# Optionally allow activating a local virtualenv by setting AC_USE_LOCAL_VENV=1.

USE_UV=0
if command -v uv >/dev/null 2>&1; then
  USE_UV=1
fi

if [ "${AC_USE_LOCAL_VENV:-0}" = "1" ]; then
  if [ -f "./venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source ./venv/bin/activate
  elif [ -f "../venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source ../venv/bin/activate
  fi
fi

# Optional: auto-sync dependencies with uv (opt-in via AC_AUTO_SYNC=1)
if [ "${AC_AUTO_SYNC:-0}" = "1" ] && [ "$USE_UV" -eq 1 ]; then
  echo "Syncing dependencies with uv..."
  uv sync
fi

RUN=""
if [ "$USE_UV" -eq 1 ]; then
  RUN="uv run"
else
  echo "[WARN] uv is not installed. Falling back to system Python's pytest.\n" \
       "       Ensure Python 3.11 is active and dependencies are installed." >&2
fi


# Check if a specific test file is provided as an argument
if [ $# -eq 1 ]; then
    SPECIFIC_TEST_FILE=$1
    if [ -f "$SPECIFIC_TEST_FILE" ]; then
        echo "Running only the specified test file: $SPECIFIC_TEST_FILE"
        $RUN pytest -v --tb=short "$SPECIFIC_TEST_FILE"
        exit $?
    else
        echo "Specified test file does not exist: $SPECIFIC_TEST_FILE"
        exit 1
    fi
fi

# Run all tests first to see which ones fail
echo "Running all tests..."
TEST_OUTPUT_FILE=$(mktemp)
$RUN pytest -v --tb=short > "$TEST_OUTPUT_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "Some tests failed. Analyzing failures..."
    
    # Show the test output
    cat "$TEST_OUTPUT_FILE"
    
    # Extract the first failed test file
    # Look for lines that start with "FAILED" and extract the test file path
    FIRST_FAILED_TEST=$(grep "^FAILED" "$TEST_OUTPUT_FILE" | head -1 | sed -E 's/^FAILED\s+([^:]+):.*/\1/')
    
    # If we didn't find a FAILED line, check for Playwright-style failures
    if [ -z "$FIRST_FAILED_TEST" ]; then
        # Look for lines with .spec.ts and extract the test file path
        FIRST_FAILED_TEST=$(grep "\.spec\.ts" "$TEST_OUTPUT_FILE" | head -1 | sed -E 's/.*([a-zA-Z0-9_/-]+\.spec\.ts).*/\1/')
    fi
    
    # If we found a failed test, run only that test
    if [ ! -z "$FIRST_FAILED_TEST" ] && [ -f "$FIRST_FAILED_TEST" ]; then
        echo "Running only the first failed test: $FIRST_FAILED_TEST"
        $RUN pytest -v --tb=short "$FIRST_FAILED_TEST"
        RESULT=$?
        rm "$TEST_OUTPUT_FILE"
        exit $RESULT
    else
        echo "Could not identify the first failed test file or file does not exist."
        rm "$TEST_OUTPUT_FILE"
        exit $EXIT_CODE
    fi
else
    echo "All tests passed!"
    rm "$TEST_OUTPUT_FILE"
    exit 0
fi