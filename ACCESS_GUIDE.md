# MCP Inspector Access Guide

## 🌐 Access URLs

### 0.0.0.0 (Recommended - External Access)
```
http://0.0.0.0:6274/?MCP_PROXY_AUTH_TOKEN=973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474
```

### localhost (Local Access Only)
```
http://localhost:6274/?MCP_PROXY_AUTH_TOKEN=973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474
```

## ✅ 0.0.0.0 Support via TCP Proxy

Using the TCP proxy (`tcp_proxy.py`), the following ports are listening on 0.0.0.0:

- **Inspector Web UI**: 0.0.0.0:6274
- **MCP Proxy Server**: 0.0.0.0:6277

### Checking Proxy Status
```bash
ps aux | grep tcp_proxy
lsof -i :6274
lsof -i :6277
```

### Testing the Proxy
```bash
# Local access (direct)
curl -s -I http://localhost:6274 | head -3

# External access (via 0.0.0.0)
curl -s -I http://0.0.0.0:6274 | head -3
```

## 🔍 Verification

### 1. Check via Web Browser
Access the above URL in your browser and verify that the MCP Inspector UI is displayed.

### 2. Check MCP Server
Verify that the "test-watcher" server is connected within Inspector. The following information should be displayed:
- Server name: test-watcher
- Available tools: 4
- Available resources: 2

### 3. Tool Testing
You can test tools directly from Inspector:
- `get_status()` - Check test watcher service status
- `start_watching()` - Start file watching

## 🔧 Troubleshooting

### When Proxy is Not Running
```bash
# Restart the proxy
pkill -f tcp_proxy.py
python3 /home/node/src/auto-coder/tcp_proxy.py 6274 localhost 6274 &
python3 /home/node/src/auto-coder/tcp_proxy.py 6277 localhost 6277 &
```

### When Connection Fails
1. Check if proxy processes are running
2. Check if ports are not in use by other processes
3. Check firewall settings

## 📊 Current Status

- **Inspector**: Running
- **TCP Proxy**: Running
- **test-watcher Server**: Configured
- **Access**: Available via 0.0.0.0

---

**Last Updated**: 2025-10-31
**Auth Token**: 973ff60cac74430ba774a6b9bbe0d366232cb452202845a6d706420fcac2f474