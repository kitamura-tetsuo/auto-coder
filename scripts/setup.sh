#!/bin/bash
set -e

retry_command() {
    local max_attempts="$1"
    local delay_seconds="$2"
    shift 2

    local attempt=1
    until "$@"; do
        if (( attempt >= max_attempts )); then
            echo "Command failed after ${attempt} attempts: $*" >&2
            return 1
        fi
        echo "Command failed (attempt ${attempt}/${max_attempts}); retrying in ${delay_seconds}s..." >&2
        sleep "$delay_seconds"
        attempt=$((attempt + 1))
        delay_seconds=$((delay_seconds * 2))
    done
}

# Change to the root directory of the repository
cd "$(dirname "$0")/.."

# Named Docker volumes are initially owned by root. Ensure uv can create .venv.
sudo mkdir -p .venv
sudo chown -R "$(id -u):$(id -g)" .venv

# GitHub SSH on port 22 is unavailable on this network; use HTTPS instead.
git config --global url."https://github.com/".insteadOf "git@github.com:"
git config --global --add url."https://github.com/".insteadOf "ssh://git@github.com/"

echo "=== Setting up development environment ==="

# Check for uv
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed. Please install uv first."
    echo "Visit https://github.com/astral-sh/uv for installation instructions."
    exit 1
fi

# Setup .env file if it doesn't exist
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo "Creating .env from .env.example..."
        cp .env.example .env
        echo "Please update .env with your configuration."
    else
        echo "Warning: .env.example not found."
    fi
fi

# Install dependencies using uv
echo "Installing dependencies with uv sync..."
uv python pin 3.12

# Use --all-extras to install optional dependencies (like test)
# Limit concurrent downloads because this Docker network has no IPv6 route and
# parallel connections can select unreachable IPv6 endpoints.
export UV_CONCURRENT_DOWNLOADS="${UV_CONCURRENT_DOWNLOADS:-1}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"
# Use --dev to install development dependencies
retry_command 5 2 uv sync --all-extras --dev

# Install pre-commit hooks
echo "Installing pre-commit hooks..."
uv run pre-commit install

echo "=== Setup complete! ==="
