#!/bin/bash
# TCP Proxy Startup Script - Robust daemon mode

set -e

LOG_FILE="/tmp/tcp_proxy_$(date +%Y%m%d_%H%M%S).log"

echo "Starting TCP Proxy Service..."
echo "Log file: $LOG_FILE"

# Function to start proxy for a port
start_proxy() {
    local listen_port=$1
    local target_host=$2
    local target_port=$3
    local proxy_name="tcp_proxy_${listen_port}"

    echo "Starting proxy: ${listen_port} -> ${target_host}:${target_port}"

    # Kill existing proxy if running
    pkill -f "tcp_proxy.py ${listen_port}" 2>/dev/null || true
    sleep 1

    # Start proxy with nohup and disown
    nohup python3 /home/node/src/auto-coder/tcp_proxy.py ${listen_port} ${target_host} ${target_port} \
        > ${LOG_FILE}_${listen_port}.log 2>&1 &

    # Disown the process to make it independent of shell
    disown %1 2>/dev/null || true

    sleep 1

    # Verify proxy is running
    if ps aux | grep -q "tcp_proxy.py ${listen_port}" | grep -v grep; then
        echo "✅ Proxy started successfully: ${listen_port}"
        return 0
    else
        echo "❌ Failed to start proxy: ${listen_port}"
        return 1
    fi
}

# Start both proxies
echo "=== Starting TCP Proxies ==="
start_proxy 6274 localhost 6274
start_proxy 6277 localhost 6277

echo ""
echo "=== Active Proxy Processes ==="
ps aux | grep tcp_proxy | grep -v grep || echo "No proxies running"

echo ""
echo "=== Port Status ==="
lsof -i :6274 -i :6277 2>/dev/null | grep -v PID || echo "No ports listening"

echo ""
echo "✅ All proxies started"
echo "Logs: ${LOG_FILE}_*.log"
echo ""

# Keep the script running to show the status
echo "Press Ctrl+C to exit..."
trap "echo 'Stopping...' ; exit 0" SIGINT
while true; do
    sleep 30
    # Periodically check if proxies are alive
    if ! ps aux | grep -q "tcp_proxy.py 6274" | grep -v grep; then
        echo "$(date): 6274 proxy died, restarting..."
        start_proxy 6274 localhost 6274
    fi
    if ! ps aux | grep -q "tcp_proxy.py 6277" | grep -v grep; then
        echo "$(date): 6277 proxy died, restarting..."
        start_proxy 6277 localhost 6277
    fi
done