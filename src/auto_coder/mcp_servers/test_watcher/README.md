# Test Watcher MCP Server

A Model Context Protocol server for continuous test monitoring. This server allows coding agents to monitor test execution in real-time without manually running tests.

## Overview

Test Watcher MCP provides a seamless integration between coding agents and continuous test execution. It supports:

- **Vitest** watch mode for JavaScript/TypeScript unit tests
- **Playwright** watch/UI mode for e2e tests
- **Pytest** watch mode for Python tests
- Real-time test result collection and reporting
- Independent operation from coding agents

This project follows the [Model Context Protocol](https://github.com/modelcontextprotocol/python-sdk) specification, making it compatible with any MCP-enabled client.

## Features

- **Continuous test monitoring** - Tests run automatically when files change
- **Multiple test framework support** - vitest, playwright, pytest
- **Real-time results** - Get latest test results without re-running tests
- **Independent operation** - Runs as a standalone server, not triggered by agents
- **MCP tools and resources** - Full integration with coding agents

## Prerequisites

- Python 3.11+
- Node.js and npm (for vitest and playwright)
- pytest-watch (for pytest monitoring): `pip install pytest-watch`

## Installation

### Quick Start

1. Create a virtual environment and install dependencies:
   ```bash
   cd src/auto_coder/mcp_servers/test_watcher
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -e .
   ```

2. Make the run script executable:
   ```bash
   chmod +x run_server.sh
   ```

### Configuration

Create a `.env` file in the test_watcher directory (optional):

```bash
# Project root directory to watch (default: current directory)
TEST_WATCHER_PROJECT_ROOT=/path/to/your/project
```

## Running the Server

### Standalone Mode

Run the server directly:

```bash
python server.py
```

Or use the run script:

```bash
./run_server.sh
```

### MCP Client Integration

Add the server to your MCP configuration file (`~/.cursor/mcp.json`, Claude Desktop config, or Windsurf config):

```json
{
  "mcpServers": {
    "test-watcher": {
      "command": "/path/to/auto-coder/src/auto_coder/mcp_servers/test_watcher/run_server.sh",
      "args": [],
      "env": {
        "TEST_WATCHER_PROJECT_ROOT": "/path/to/your/project"
      }
    }
  }
}
```

Restart your MCP client (Cursor, Claude Desktop, Windsurf, etc.)

## Usage

### MCP Tools

This server provides the following tools for coding agents:

#### 1. `start_vitest_watch` - Start vitest in watch mode

```python
# Start vitest watcher
result = start_vitest_watch()

# With custom config
result = start_vitest_watch(config_path="vitest.config.ts")
```

#### 2. `start_playwright_watch` - Start playwright in watch mode

```python
# Start playwright in UI mode (default)
result = start_playwright_watch()

# Start in watch mode without UI
result = start_playwright_watch(ui_mode=False)
```

#### 3. `start_pytest_watch` - Start pytest in watch mode

```python
# Watch all tests
result = start_pytest_watch()

# Watch specific test path
result = start_pytest_watch(test_path="tests/unit")
```

#### 4. `get_test_results` - Get latest test results

```python
# Get all test results
results = get_test_results()

# Get results from specific watcher
results = get_test_results(watcher_id="vitest")
```

Example result:
```json
{
  "watcher_id": "vitest",
  "results": {
    "last_updated": "2025-10-27T10:30:45.123456",
    "status": "running",
    "total_tests": 42,
    "passed": 40,
    "failed": 2,
    "skipped": 0,
    "errors": [
      "FAIL src/utils.test.ts > calculateSum > should handle negative numbers",
      "Error: Expected 5 to equal -5"
    ],
    "output_lines": [
      "Test Files  5 passed (5)",
      "Tests  40 passed | 2 failed (42)",
      "..."
    ]
  }
}
```

#### 5. `get_watcher_status` - Get watcher status

```python
# Get all watcher statuses
status = get_watcher_status()

# Get specific watcher status
status = get_watcher_status(watcher_id="vitest")
```

#### 6. `stop_watcher` - Stop a watcher

```python
# Stop specific watcher
result = stop_watcher(watcher_id="vitest")
```

### MCP Resources

#### 1. `test-watcher://status` - Overall status

Get a formatted overview of all watchers and their results:

```python
status = read_resource("test-watcher://status")
```

#### 2. `test-watcher://help` - Help information

Get usage instructions:

```python
help_text = read_resource("test-watcher://help")
```

## Typical Workflow

### For Coding Agents

1. **Start watchers** when beginning a coding session:
   ```python
   start_vitest_watch()
   start_playwright_watch()
   ```

2. **Check results** periodically or after making changes:
   ```python
   results = get_test_results()
   if results["all_results"]["vitest"]["failed"] > 0:
       # Analyze and fix failures
       errors = results["all_results"]["vitest"]["errors"]
   ```

3. **Monitor status** to ensure watchers are running:
   ```python
   status = get_watcher_status()
   ```

4. **Stop watchers** when done:
   ```python
   stop_watcher(watcher_id="vitest")
   stop_watcher(watcher_id="playwright")
   ```

### For Manual Testing

You can also interact with the server manually using MCP clients or by calling the tools directly through the MCP protocol.

## Architecture

```
┌─────────────────┐
│ Coding Agent    │
│ (Cursor/Claude) │
└────────┬────────┘
         │ MCP Protocol
         │
┌────────▼────────────────────┐
│ Test Watcher MCP Server     │
│                             │
│ ┌─────────────────────────┐ │
│ │ TestWatcherTool         │ │
│ │ - Manages watchers      │ │
│ │ - Collects results      │ │
│ └─────────────────────────┘ │
└──┬──────┬──────┬───────────┘
   │      │      │
   ▼      ▼      ▼
┌──────┐ ┌──────┐ ┌──────┐
│vitest│ │pwt   │ │pytest│
│watch │ │--ui  │ │-watch│
└──────┘ └──────┘ └──────┘
```

## Troubleshooting

### Watcher won't start

- Ensure the test framework is installed in your project
- Check that the project root is correctly set
- Verify that the test commands work manually

### No test results

- Wait a few seconds after starting the watcher for initial results
- Check watcher status to ensure it's running
- Look at the output_lines in the results for diagnostic information

### Watcher stopped unexpectedly

- Check the server.log file for error messages
- Ensure the test process didn't crash due to configuration issues
- Restart the watcher

## Development

### Running Tests

```bash
pytest tests/
```

### Contributing

Contributions are welcome! Please ensure:

- All tests pass
- Code follows the existing style
- New features include tests and documentation

## License

This project is part of the auto-coder project and follows the same license.

## Related Projects

- [Model Context Protocol](https://github.com/modelcontextprotocol/python-sdk)
- [FastMCP](https://github.com/jlowin/fastmcp)
- [Vitest](https://vitest.dev/)
- [Playwright](https://playwright.dev/)
- [pytest-watch](https://github.com/joeyespo/pytest-watch)

