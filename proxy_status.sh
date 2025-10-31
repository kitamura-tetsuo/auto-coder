#!/bin/bash
# Proxy Status Check Script

echo "=== TCP Proxy Status Report ==="
echo "Timestamp: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

echo "--- Running Processes ---"
ps aux | grep tcp_proxy | grep -v grep || echo "No TCP proxy processes running"
echo ""

echo "--- Port Status ---"
lsof -i :6274 2>/dev/null | grep -v PID | grep LISTEN || echo "Port 6274: Not listening"
lsof -i :6277 2>/dev/null | grep -v PID | grep LISTEN || echo "Port 6277: Not listening"
echo ""

echo "--- Connection Tests ---"
if curl -s -I http://0.0.0.0:6274 > /dev/null 2>&1; then
    echo "✅ Port 6274: Accessible"
else
    echo "❌ Port 6274: Not accessible"
fi

if curl -s -I http://0.0.0.0:6277 > /dev/null 2>&1; then
    echo "✅ Port 6277: Accessible"
else
    echo "❌ Port 6277: Not accessible"
fi
echo ""

echo "--- Monitor Status ---"
if ps aux | grep -q "monitor_proxy.sh" | grep -v grep; then
    echo "✅ Monitor: Running"
else
    echo "❌ Monitor: Not running"
fi
echo ""

echo "--- PID Files ---"
if [ -f "/tmp/tcp_proxy_6274.pid" ]; then
    pid=$(cat /tmp/tcp_proxy_6274.pid)
    if ps -p "$pid" > /dev/null 2>&1; then
        echo "✅ Port 6274 PID file: $pid (active)"
    else
        echo "⚠️  Port 6274 PID file: $pid (stale)"
    fi
else
    echo "❌ Port 6274 PID file: Not found"
fi

if [ -f "/tmp/tcp_proxy_6277.pid" ]; then
    pid=$(cat /tmp/tcp_proxy_6277.pid)
    if ps -p "$pid" > /dev/null 2>&1; then
        echo "✅ Port 6277 PID file: $pid (active)"
    else
        echo "⚠️  Port 6277 PID file: $pid (stale)"
    fi
else
    echo "❌ Port 6277 PID file: Not found"
fi
echo ""

echo "--- Failure Counters ---"
failures_6274=$(cat /tmp/tcp_proxy_6274_failures 2>/dev/null || echo "0")
failures_6277=$(cat /tmp/tcp_proxy_6277_failures 2>/dev/null || echo "0")
echo "Port 6274 failures: $failures_6274"
echo "Port 6277 failures: $failures_6277"
echo ""

echo "--- Recent Monitor Log (last 10 lines) ---"
tail -10 /tmp/proxy_monitor.log 2>/dev/null || echo "Monitor log not found"