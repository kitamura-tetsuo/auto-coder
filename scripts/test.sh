#!/bin/bash

# Activate virtual environment if it exists
if [ -f "./venv/bin/activate" ]; then
    source ./venv/bin/activate
elif [ -f "../venv/bin/activate" ]; then
    source ../venv/bin/activate
fi

# Check if a specific test file is provided as an argument
if [ $# -eq 1 ]; then
    SPECIFIC_TEST_FILE=$1
    if [ -f "$SPECIFIC_TEST_FILE" ]; then
        echo "Running only the specified test file: $SPECIFIC_TEST_FILE"
        uv run pytest -v --tb=short "$SPECIFIC_TEST_FILE"
        exit $?
    else
        echo "Specified test file does not exist: $SPECIFIC_TEST_FILE"
        exit 1
    fi
fi

# Run all tests first to see which ones fail
echo "Running all tests..."
TEST_OUTPUT_FILE=$(mktemp)
uv run pytest -v --tb=short > "$TEST_OUTPUT_FILE" 2>&1
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
        uv run pytest -v --tb=short "$FIRST_FAILED_TEST"
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