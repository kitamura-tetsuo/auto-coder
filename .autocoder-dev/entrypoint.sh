#!/bin/bash
set -e

# Ensure SSHD run directory exists
sudo mkdir -p /var/run/sshd

# Start SSHD in background
sudo /usr/sbin/sshd

# Setup Tailscale if TS_AUTHKEY is provided
if [ -n "${TS_AUTHKEY:-}" ]; then
    echo "Setting up Tailscale..."
    sudo mkdir -p /var/run/tailscale
    # In some environments, we might need --tun=userspace-networking
    sudo tailscaled --tun=userspace-networking --socks5-server=localhost:1055 &
    sleep 2
    sudo tailscale up --authkey=${TS_AUTHKEY} --hostname=auto-coder-${TARGET:-app} || true
    # Optional funnel
    # sudo tailscale funnel 8080 &
fi

# If the command is just /bin/bash, we ignore it so process-issues-watcher uses its default behavior (watching current dir)
if [ "$1" = "/bin/bash" ]; then
    shift
fi

exec process-issues-watcher --opts="--disable-graphrag --force" "$@"
