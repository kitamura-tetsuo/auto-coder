# MCP-PDB Setup Complete

## Overview
The MCP-PDB (Model Context Protocol Python Debugger) tool is now available in this workspace.
This tool enables the use of Python debugger (pdb) through MCP from LLMs such as Claude.

## Setup Status

### ✅ Completed Tasks
1. **Python 3.13 Installation**: MCP-PDB requires Python 3.13 or higher
2. **MCP-PDB Tool Installation**: Installation completed using uv
3. **Server Startup**: MCP-PDB server is running normally
4. **Verification Confirmed**: Python code execution verified with test files

### 🔧 Current Status
- **MCP-PDB Server**: Running (Terminal ID: 57)
- **Python Runtime**: Python 3.13.6
- **Working Directory**: `/home/ubuntu/src/auto-coder`
- **Test File**: `test_debug_sample.py` created

## How to Use

### 1. Windsurf Configuration
Add the following to settings.json:

```json
{
  "mcpServers": {
    "mcp-pdb": {
      "command": "uv",
      "args": [
        "run",
        "--python",
        "3.13",
        "--with",
        "mcp-pdb",
        "mcp-pdb"
      ]
    }
  }
}
```

### 2. Claude Code Configuration
Run the following command:

```bash
claude mcp add mcp-pdb -- uv run --python 3.13 --with mcp-pdb mcp-pdb
```

### 3. Available Tools

| Tool | Description |
|------|-------------|
| `start_debug(file_path, use_pytest, args)` | Start debugging session for a Python file |
| `send_pdb_command(command)` | Send command to running PDB instance |
| `set_breakpoint(file_path, line_number)` | Set breakpoint at specific line |
| `clear_breakpoint(file_path, line_number)` | Clear breakpoint at specific line |
| `list_breakpoints()` | List all current breakpoints |
| `restart_debug()` | Restart current debugging session |
| `examine_variable(variable_name)` | Get detailed information about a variable |
| `get_debug_status()` | Display current state of debugging session |
| `end_debug()` | End current debugging session |

### 4. Commonly Used PDB Commands

| Command | Description |
|---------|-------------|
| `n` | Next line (step over) |
| `s` | Step into function |
| `c` | Continue execution |
| `r` | Return from current function |
| `p variable` | Print variable value |
| `pp variable` | Pretty-print variable |
| `l` | List source code |
| `q` | Quit debugging |

## Test File

`test_debug_sample.py` has been created and can be used to test the following features:
- Factorial calculation function
- Fibonacci sequence calculation function
- Error handling

## Notes

⚠️ **Security Warning**: This tool executes Python code through a debugger. Use only in trusted environments.

## Server Management

### Check Server Status
```bash
# Check process list
ps aux | grep mcp-pdb
```

### Stop Server (if needed)
To stop the currently running MCP-PDB server, terminate the process with Terminal ID 57.

### Restart Server
```bash
uv run --python 3.13 --with mcp-pdb mcp-pdb
```

## Next Steps

1. **IDE Configuration**: Add the above settings in Windsurf or Claude Code
2. **Verify Connection**: Confirm connection with MCP server
3. **Start Debugging**: Begin debugging session using the `start_debug()` tool

The MCP-PDB tool has been successfully set up and Python code debugging is now available.
