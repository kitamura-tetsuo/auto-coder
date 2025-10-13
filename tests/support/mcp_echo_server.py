import json
import sys


def _read_headers(stdin):
    data = b""
    while True:
        line = stdin.buffer.readline()
        if not line:
            return None
        data += line
        if data.endswith(b"\r\n\r\n"):
            break
    headers = {}
    for h in data.decode("utf-8", errors="ignore").split("\r\n"):
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


essential_server_info = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "mcp-echo-server", "version": "0.0.2"},
}


def _send(obj):
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(b)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(b)
    sys.stdout.buffer.flush()


def main():
    while True:
        headers = _read_headers(sys.stdin)
        if headers is None:
            break
        try:
            length = int(headers.get("content-length", "0"))
        except Exception:
            length = 0
        body = sys.stdin.buffer.read(length)
        if not body:
            break
        try:
            msg = json.loads(body.decode("utf-8"))
        except Exception:
            # Ignore malformed
            continue

        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": essential_server_info})
        elif method == "prompts/call":
            name = (params or {}).get("name")
            arguments = (params or {}).get("arguments") or {}
            if name == "default":
                text = str(arguments.get("input", ""))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {
                            "content": [{"type": "text", "text": f"PROMPT: {text}"}]
                        },
                    }
                )
            else:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32601, "message": f"Unknown prompt: {name}"},
                    }
                )
        elif method == "inference/create":
            arguments = (params or {}).get("arguments") or {}
            text = str(arguments.get("input", ""))
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"content": [{"type": "text", "text": f"INFER: {text}"}]},
                }
            )
        elif method == "tools/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "tools": [
                            {
                                "name": "run",
                                "description": "Run a prompt as a single-shot command",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "input": {"type": "string"},
                                    },
                                    "required": ["text"],
                                },
                            },
                            {
                                "name": "execute",
                                "description": "Alias of run",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "input": {"type": "string"},
                                    },
                                    "required": ["text"],
                                },
                            },
                            {
                                "name": "workspace-write",
                                "description": "Write to workspace based on prompt",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "input": {"type": "string"},
                                    },
                                    "required": ["text"],
                                },
                            },
                            {
                                "name": "echo",
                                "description": "Echo back text",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            },
                        ]
                    },
                }
            )
        elif method == "tools/call":
            name = (params or {}).get("name")
            arguments = (params or {}).get("arguments") or {}
            if name in ("run", "execute", "workspace-write"):
                text = str(arguments.get("text") or arguments.get("input", ""))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {
                            "content": [{"type": "text", "text": f"RUN: {text}"}]
                        },
                    }
                )
            elif name == "echo":
                text = str(arguments.get("text", ""))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {
                            "content": [{"type": "text", "text": f"ECHO: {text}"}]
                        },
                    }
                )
            else:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                    }
                )
        else:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
