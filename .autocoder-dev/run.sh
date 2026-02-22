#!/bin/bash
set -e

# Configuration
num=10
export SSH_PORT=$(( num + 2222 ))
export DOCKER_GID=$(getent group docker | cut -d: -f3 || echo 999)

# Collect SSH keys from host if they exist to pass as build-arg
# (Non-sensitive part of the setup)
if [ -f "$HOME/.ssh/authorized_keys" ]; then
    export AUTHORIZED_KEYS=$(cat "$HOME/.ssh/authorized_keys")
elif [ -d "$HOME/.ssh" ]; then
    export AUTHORIZED_KEYS=$(cat "$HOME/.ssh"/*.pub 2>/dev/null || true)
fi

echo "Rebuilding auto-coder-env with DOCKER_GID=$DOCKER_GID..."

docker compose -f docker-compose.yml build --no-cache \
    --build-arg DOCKER_GID="$DOCKER_GID" \
    --build-arg AUTHORIZED_KEYS="$AUTHORIZED_KEYS"

echo "Starting auto-coder-env..."
docker compose -f docker-compose.yml up -d

echo "Container started. Logs:"
docker logs -f auto-coder-env
