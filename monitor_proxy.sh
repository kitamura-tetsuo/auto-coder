#!/bin/bash
# TCP Proxy Monitor and Auto-Restart Script

LOG_FILE="/tmp/proxy_monitor.log"
RESTART_THRESHOLD=3  # Number of consecutive failures before alerting

# Function to log messages
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Function to start proxy for a specific port
start_proxy() {
    local port=$1
    local target_port=$2
    local pid_file="/tmp/tcp_proxy_${port}.pid"
    local log_file="/tmp/tcp_proxy_${port}.log"

    log "Attempting to start proxy for port $port..."

    # Check if already running
    if [ -f "$pid_file" ]; then
        old_pid=$(cat "$pid_file")
        if ps -p "$old_pid" > /dev/null 2>&1; then
            log "Proxy for port $port is already running (PID: $old_pid)"
            return 0
        else
            log "Stale PID file found for port $port, removing..."
            rm -f "$pid_file"
        fi
    fi

    # Kill any existing processes on the port
    pkill -f "tcp_proxy.py $port localhost" 2>/dev/null || true
    sleep 1

    # Start new proxy process
    nohup python3 /home/node/src/auto-coder/tcp_proxy.py "$port" localhost "$target_port" > "$log_file" 2>&1 &
    new_pid=$!

    # Save PID
    echo "$new_pid" > "$pid_file"

    # Verify it started successfully
    sleep 2
    if ps -p "$new_pid" > /dev/null 2>&1; then
        log "✅ Proxy for port $port started successfully (PID: $new_pid)"
        echo 0 > "/tmp/tcp_proxy_${port}_failures"  # Reset failure counter
        return 0
    else
        log "❌ Failed to start proxy for port $port"
        echo 1 > "/tmp/tcp_proxy_${port}_failures"  # Set failure counter
        return 1
    fi
}

# Function to check and restart proxy
check_and_restart() {
    local port=$1
    local target_port=$2
    local pid_file="/tmp/tcp_proxy_${port}.pid"
    local failures_file="/tmp/tcp_proxy_${port}_failures"

    # Get current failure count
    local failures=0
    if [ -f "$failures_file" ]; then
        failures=$(cat "$failures_file")
    fi

    # Check if process is running
    if [ -f "$pid_file" ]; then
        pid=$(cat "$pid_file")
        if ps -p "$pid" > /dev/null 2>&1; then
            # Process is running, reset failure counter
            if [ "$failures" -gt 0 ]; then
                log "Process for port $port recovered (was failing $failures times)"
            fi
            echo 0 > "$failures_file"
            return 0
        fi
    fi

    # Process is not running, increment failure counter
    failures=$((failures + 1))
    echo "$failures" > "$failures_file"

    log "⚠️  Proxy for port $port is not running (failure #$failures)"

    if [ "$failures" -ge "$RESTART_THRESHOLD" ]; then
        log "🔄 Multiple failures detected for port $port, restarting..."
        start_proxy "$port" "$target_port"
    else
        log "Attempting to restart proxy for port $port..."
        start_proxy "$port" "$target_port"
    fi
}

# Main monitoring loop
main() {
    log "=== TCP Proxy Monitor Started ==="
    log "Monitoring ports 6274 and 6277"
    log "Restart threshold: $RESTART_THRESHOLD failures"

    # Initialize failure counters
    echo 0 > "/tmp/tcp_proxy_6274_failures"
    echo 0 > "/tmp/tcp_proxy_6277_failures"

    # Start proxies initially
    start_proxy 6274 6274
    start_proxy 6277 6277

    log "Initial startup complete. Entering monitoring loop..."

    # Monitor in a loop
    while true; do
        check_and_restart 6274 6274
        check_and_restart 6277 6277

        # Wait before next check
        sleep 10
    done
}

# Run main function
main