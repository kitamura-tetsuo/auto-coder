#!/bin/bash
set -e

# Change to the root directory of the repository
cd "$(dirname "$0")/.."

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
# Use --all-extras to install optional dependencies (like test)
# Use --dev to install development dependencies
uv sync --all-extras --dev

# Install pre-commit hooks
echo "Installing pre-commit hooks..."
uv run pre-commit install

echo "=== Setup complete! ==="
