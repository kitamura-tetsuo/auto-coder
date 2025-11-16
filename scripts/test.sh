#!/bin/bash
set -Eeuo pipefail

# -----------------------------------------------------------------------------
# Environment detection and setup
# -----------------------------------------------------------------------------
# Detect CI environment and set appropriate flags

echo "Detecting CI environment..."

# Check if running in CI
IS_CI=0
if [ "${GITHUB_ACTIONS:-}" = "true" ] || [ "${CI:-}" = "true" ]; then
    IS_CI=1
    echo "[INFO] Running in CI environment (GitHub Actions or other CI)"
else
    echo "[INFO] Running in local development environment"
fi

# Check if running in a container
IN_CONTAINER=0
if [ -f /.dockerenv ] || [ -f /run/.containerenv ]; then
    IN_CONTAINER=1
    echo "[INFO] Running inside a container"
else
    echo "[INFO] Running on host system"
fi

# Set test environment isolation variables
export AUTOCODER_TEST_IN_CONTAINER="${IN_CONTAINER}"
export AUTOCODER_TEST_IS_CI="${IS_CI}"

echo ""
echo "Environment configuration:"
echo "  CI environment: ${IS_CI}"
echo "  Container: ${IN_CONTAINER}"
echo ""
# -----------------------------------------------------------------------------
# Pytest verbosity flags
# -----------------------------------------------------------------------------
if [ "$IS_CI" -eq 1 ]; then
    PYTEST_ALL_FLAGS="-vv"
    PYTEST_SINGLE_FLAGS="-vv"
else
    PYTEST_ALL_FLAGS="-q"
    PYTEST_SINGLE_FLAGS="-v"
fi

# -----------------------------------------------------------------------------
# Dependency checking
# -----------------------------------------------------------------------------
# Check for required CLI dependencies and provide helpful warnings

check_cli_dependency() {
    local cmd=$1
    local name=$2
    local optional=${3:-false}

    if command -v "$cmd" >/dev/null 2>&1; then
        echo "[INFO] $name is available"
        return 0
    else
        if [ "$optional" = "true" ]; then
            echo "[WARN] $name is not available (optional dependency)"
            return 1
        else
            echo "[ERROR] $name is not available (required dependency)"
            return 2
        fi
    fi
}

echo "Checking CLI dependencies..."
CLI_DEPS_OK=0

# Check for Node.js (optional, used by graph-builder TypeScript CLI)
if check_cli_dependency "node" "Node.js" "true"; then
    NODE_VERSION=$(node --version 2>/dev/null || echo "unknown")
    echo "       Node.js version: $NODE_VERSION"
fi

# Check for Python 3 (required)
if ! check_cli_dependency "python3" "Python 3" "false"; then
    CLI_DEPS_OK=1
fi

PYTHON_VERSION=$(python3 --version 2>/dev/null || echo "unknown")
echo "       Python version: $PYTHON_VERSION"

# Check for graph-builder (optional)
GRAPH_BUILDER_FOUND=false
if [ -d "./src/auto_coder/graph_builder" ]; then
    echo "[INFO] graph-builder found in ./src/auto_coder/graph_builder"
    GRAPH_BUILDER_FOUND=true
elif [ -d "./graph-builder" ]; then
    echo "[INFO] graph-builder found in ./graph-builder"
    GRAPH_BUILDER_FOUND=true
elif [ -d "$HOME/graph-builder" ]; then
    echo "[INFO] graph-builder found in $HOME/graph-builder"
    GRAPH_BUILDER_FOUND=true
else
    echo "[WARN] graph-builder not found in common locations"
    echo "       Searched: ./src/auto_coder/graph_builder, ./graph-builder, ~/graph-builder"
fi

# Summary
echo ""
echo "Dependency check summary:"
if [ $CLI_DEPS_OK -eq 0 ]; then
    echo "[OK] Core dependencies are available"
else
    echo "[ERROR] Some required dependencies are missing"
fi

if [ "$GRAPH_BUILDER_FOUND" = "true" ]; then
    echo "[OK] graph-builder is available"
else
    echo "[INFO] graph-builder not found - tests will use fallback Python indexing"
fi

echo ""

# Continue with test execution even if some optional dependencies are missing
if [ $CLI_DEPS_OK -ne 0 ]; then
    echo "[ERROR] Cannot continue without required dependencies"
    exit 1
fi

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

# Always sync dependencies with uv when available
# Skip sync in CI to avoid conflicts with pre-synced environment
if [ "$USE_UV" -eq 1 ] && [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${CI:-}" != "true" ]; then
  uv sync -q
  uv pip install -q -e .[test]
fi

RUN=""
if [ "$USE_UV" -eq 1 ]; then
  RUN="uv run"
else
  printf "[WARN] uv is not installed. Falling back to system Python's pytest.\n"
  printf "       Ensure Python 3.11 is active and dependencies are installed.\n" >&2
fi


# Check if a specific test file is provided as an argument
if [ $# -ge 1 ]; then
    SPECIFIC_TEST_FILE=$1
    shift  # Remove first argument
    if [ -f "$SPECIFIC_TEST_FILE" ]; then
        echo "Running only the specified test file: $SPECIFIC_TEST_FILE"
        # Don't generate HTML coverage report for single test files (faster)
        $RUN pytest $PYTEST_SINGLE_FLAGS --tb=short --timeout=60 --cov=src/auto_coder --cov-report=term-missing "$SPECIFIC_TEST_FILE" "$@"
        exit $?
    else
        echo "Specified test file does not exist: $SPECIFIC_TEST_FILE"
        exit 1
    fi
fi

# Run all tests first to see which ones fail
echo "Running all tests..."
TEST_OUTPUT_FILE=$(mktemp)

# Don't exit on errors - we want to capture the exit code
set +e

$RUN pytest $PYTEST_ALL_FLAGS --tb=short --timeout=60 --cov=src/auto_coder --cov-report=html --cov-report=term-missing | tee "$TEST_OUTPUT_FILE"
EXIT_CODE=${PIPESTATUS[0]}

# Re-enable exit on errors
set -e

echo "Test run completed with exit code: $EXIT_CODE"

if [ $EXIT_CODE -ne 0 ]; then
    echo "Some tests failed. Analyzing failures..."
    
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
        $RUN pytest $PYTEST_SINGLE_FLAGS --tb=short --timeout=60 --cov=src/auto_coder --cov-report=term-missing "$FIRST_FAILED_TEST"
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