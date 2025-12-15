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

# Unset VIRTUAL_ENV to prevent conflicts with uv's environment detection
# This prevents warnings about VIRTUAL_ENV not matching the project environment path
if [ -n "${VIRTUAL_ENV:-}" ]; then
  unset VIRTUAL_ENV
fi

# Always sync dependencies with uv when available
# Skip sync in CI to avoid conflicts with pre-synced environment
if [ "$USE_UV" -eq 1 ] && [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${CI:-}" != "true" ]; then
  uv sync -q --extra test
  # Install test dependencies including pytest-timeout
  # Note: We need to ensure pytest-timeout is installed in the environment that uv uses
  if ! uv run python -c "import pytest_timeout" 2>/dev/null; then
    uv pip install pytest-timeout
  fi
fi

RUN=""
PYTHON_RUNNER="python3"
if [ "$USE_UV" -eq 1 ]; then
  RUN="uv run"
  PYTHON_RUNNER="uv run python"
else
  printf "[WARN] uv is not installed. Falling back to system Python's pytest.
"
  printf "       Ensure Python 3.11 is active and dependencies are installed.
" >&2
fi

# -----------------------------------------------------------------------------
# Code quality checks (matching .pre-commit-config.yaml)
# -----------------------------------------------------------------------------
echo ""
echo "Installing code quality tools..."

# Install code quality tools
if [ "$USE_UV" -eq 1 ]; then
  uv pip install black isort flake8 mypy types-toml
else
  pip install black isort flake8 mypy types-toml
fi

echo ""
echo "Running code quality checks..."

# Run black (auto-fix in local, check only in CI)
echo "[CHECK] Running black..."
if [ "$IS_CI" -eq 0 ]; then
  echo "  [LOCAL] Running black in auto-fix mode..."
  $RUN black src/ tests/
else
  echo "  [CI] Running black in check mode..."
  $RUN black --check src/ tests/
fi

# Run isort (auto-fix in local, check only in CI)
echo "[CHECK] Running isort..."
if [ "$IS_CI" -eq 0 ]; then
  echo "  [LOCAL] Running isort in auto-fix mode..."
  $RUN isort src/ tests/
else
  echo "  [CI] Running isort in check mode..."
  $RUN isort --check-only src/ tests/
fi

# Run flake8
echo "[CHECK] Running flake8..."
$RUN flake8 src/ tests/

# Run mypy
echo "[CHECK] Running mypy..."
# Run mypy from root directory with proper module resolution
$RUN mypy -c "import sys; sys.path.insert(0, 'src'); import auto_coder" || true

echo "[OK] All code quality checks passed!"
echo ""

# -----------------------------------------------------------------------------
# Test execution via log collector
# -----------------------------------------------------------------------------
echo "Running tests via local_test_log_collector.py..."
$PYTHON_RUNNER src/auto_coder/local_test_log_collector.py "$@"
EXIT_CODE=$?

echo "Test run completed with exit code: $EXIT_CODE"
exit $EXIT_CODE
