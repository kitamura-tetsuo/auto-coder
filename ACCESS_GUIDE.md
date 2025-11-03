# MCP Inspector Access Guide

## 🌐 Access URLs

### 0.0.0.0 (Recommended - Accessible from External)
```
http://0.0.0.0:6274/?MCP_PROXY_AUTH_TOKEN=973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474
```

### localhost (Local Access Only)
```
http://localhost:6274/?MCP_PROXY_AUTH_TOKEN=973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474
```

## ✅ TCP Proxy Support for 0.0.0.0

Using the TCP proxy (`tcp_proxy.py`), the following ports are listening on 0.0.0.0:

- **Inspector Web UI**: 0.0.0.0:6274
- **MCP Proxy Server**: 0.0.0.0:6277

### Proxy Status Check
```bash
ps aux | grep tcp_proxy
lsof -i :6274
lsof -i :6277
```

### Proxy Testing
```bash
# Local Access (Direct)
curl -s -I http://localhost:6274 | head -3

# External Access (via 0.0.0.0)
curl -s -I http://0.0.0.0:6274 | head -3
```

## 🔍 Verification

### 1. Web Browser Verification
Access the above URLs in your web browser to confirm the MCP Inspector UI is displayed.

### 2. MCP Server Verification
Verify that the "test-watcher" server is connected in the Inspector. The following information should be displayed:
- Server Name: test-watcher
- Available Tools: 4
- Available Resources: 2

### 3. Tool Testing
You can test tools directly from the Inspector:
- `get_status()` - Check the status of the test monitoring service
- `start_watching()` - Start file watching

## 🔧 Troubleshooting

### When Proxy is Not Running
```bash
# Restart proxy
pkill -f tcp_proxy.py
python3 /home/node/src/auto-coder/tcp_proxy.py 6274 localhost 6274 &
python3 /home/node/src/auto-coder/tcp_proxy.py 6277 localhost 6277 &
```

### When Unable to Connect
1. Check if the proxy process is running
2. Check if ports are in use by other processes
3. Check firewall settings

## 📊 Current Status

- **Inspector**: Running
- **TCP Proxy**: Running
- **test-watcher Server**: Configured
- **Access**: Available via 0.0.0.0

---

**Last Updated**: 2025-10-31
**Auth Token**: 973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474