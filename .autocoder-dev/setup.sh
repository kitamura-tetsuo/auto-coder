#!/bin/bash
set -e

echo "Ensuring auto-coder-env dependencies are installed..."
docker exec -it auto-coder-env uv sync --all-extras --dev

echo "Setup completed successfully."
