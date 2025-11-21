"""Tests for GraphRAG MCP server connection.

This test verifies that we can connect to a real graphrag MCP server
and perform basic operations.

Note: These tests use the real HOME directory and are not affected by
the conftest.py autouse fixture that mocks HOME for other tests.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from auto_coder.logger_config import get_logger

logger = get_logger(__name__)

# Mark all tests in this module to use real HOME directory and real commands
pytestmark = pytest.mark.usefixtures("_use_real_home", "_use_real_commands")


def find_graphrag_mcp_server() -> Path | None:
    """Find graphrag_mcp server installation.

    Returns:
        Path to graphrag_mcp main.py if found, None otherwise
    """
    # Check common installation locations
    possible_paths = [
        Path.home() / "graphrag_mcp" / "main.py",
        Path.home() / ".local" / "share" / "graphrag_mcp" / "main.py",
        Path("/opt/graphrag_mcp/main.py"),
    ]

    # Also check GRAPHRAG_MCP_SERVER_PATH environment variable
    env_path = os.environ.get("GRAPHRAG_MCP_SERVER_PATH")
    if env_path:
        # Extract path from command like "uv run /path/to/main.py"
        parts = env_path.split()
        for part in parts:
            if part.endswith("main.py"):
                possible_paths.insert(0, Path(part))

    for path in possible_paths:
        logger.debug(f"Checking graphrag_mcp path: {path} (exists: {path.exists()})")
        if path.exists():
            logger.info(f"Found graphrag_mcp server at: {path}")
            return path

    logger.warning(f"graphrag_mcp server not found. Checked paths: {possible_paths}")
    return None


def check_graphrag_dependencies() -> bool:
    """Check if graphrag_mcp dependencies are available.

    Returns:
        True if dependencies are available, False otherwise
    """
    # Check if uv is available
    try:
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.warning("uv is not available")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("uv is not available")
        return False

    return True


def check_graphrag_env_config(server_path: Path) -> bool:
    """Check if graphrag_mcp .env file is configured.

    Args:
        server_path: Path to graphrag_mcp main.py

    Returns:
        True if .env file exists and has required variables, False otherwise
    """
    env_file = server_path.parent / ".env"
    if not env_file.exists():
        logger.warning(f".env file not found at: {env_file}")
        return False

    # Check for required environment variables
    required_vars = ["NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "QDRANT_URL"]
    env_content = env_file.read_text()

    for var in required_vars:
        if var not in env_content:
            logger.warning(f"Required variable {var} not found in .env file")
            return False

    return True


@pytest.fixture
def graphrag_server_path():
    """Fixture to find graphrag_mcp server path."""
    server_path = find_graphrag_mcp_server()
    if server_path is None:
        pytest.skip("graphrag_mcp server not found. Run 'auto-coder graphrag setup-mcp' to install.")
    return server_path


@pytest.fixture
def graphrag_server_process(graphrag_server_path):
    """Fixture to start graphrag_mcp server process.

    Yields:
        subprocess.Popen: Running MCP server process
    """
    if not check_graphrag_dependencies():
        pytest.skip("graphrag_mcp dependencies not available")

    if not check_graphrag_env_config(graphrag_server_path):
        pytest.skip("graphrag_mcp .env configuration not found or incomplete")

    # Start MCP server
    cmd = ["uv", "run", str(graphrag_server_path)]
    logger.info(f"Starting graphrag_mcp server: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=graphrag_server_path.parent,
    )

    # Give server time to start
    time.sleep(2)

    # Check if process is still running
    if process.poll() is not None:
        stderr = process.stderr.read().decode() if process.stderr else ""
        pytest.fail(f"graphrag_mcp server failed to start. stderr: {stderr}")

    yield process

    # Cleanup
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def send_jsonrpc_message(process: subprocess.Popen, message: dict) -> dict:
    """Send JSON-RPC message to MCP server and read response.

    Args:
        process: MCP server process
        message: JSON-RPC message to send

    Returns:
        JSON-RPC response
    """
    # Send message
    message_str = json.dumps(message) + "\n"
    logger.debug(f"Sending: {message_str.strip()}")
    assert process.stdin is not None
    process.stdin.write(message_str.encode())
    process.stdin.flush()

    # Read response
    assert process.stdout is not None
    response_line = process.stdout.readline().decode().strip()
    logger.debug(f"Received: {response_line}")

    if not response_line:
        raise RuntimeError("No response from MCP server")

    return json.loads(response_line)


def test_graphrag_mcp_server_starts(graphrag_server_process):
    """Test that graphrag_mcp server starts successfully."""
    assert graphrag_server_process.poll() is None, "Server process should be running"


def test_graphrag_mcp_initialize(graphrag_server_process):
    """Test MCP initialize handshake."""
    # Send initialize request
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "auto-coder-test", "version": "1.0.0"},
        },
    }

    response = send_jsonrpc_message(graphrag_server_process, init_request)

    # Verify response
    assert "result" in response or "error" not in response, f"Initialize failed: {response}"
    assert response.get("id") == 1, "Response ID should match request ID"


def test_graphrag_mcp_list_tools(graphrag_server_process):
    """Test listing available MCP tools."""
    # First initialize
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "auto-coder-test", "version": "1.0.0"},
        },
    }
    send_jsonrpc_message(graphrag_server_process, init_request)

    # List tools
    list_tools_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }

    response = send_jsonrpc_message(graphrag_server_process, list_tools_request)

    # Verify response
    assert "result" in response or "error" not in response, f"List tools failed: {response}"
    assert response.get("id") == 2, "Response ID should match request ID"

    # Check for expected tools
    if "result" in response:
        tools = response["result"].get("tools", [])
        logger.info(f"Available tools: {[t.get('name') for t in tools]}")
        # graphrag_mcp should provide search-related tools
        assert len(tools) > 0, "Should have at least one tool available"


def test_graphrag_mcp_connection_from_python():
    """Test connecting to graphrag_mcp server from Python code.

    This test verifies the connection without using fixtures,
    simulating how the actual code would connect.
    """
    server_path = find_graphrag_mcp_server()
    if server_path is None:
        pytest.skip("graphrag_mcp server not found")

    if not check_graphrag_dependencies():
        pytest.skip("graphrag_mcp dependencies not available")

    if not check_graphrag_env_config(server_path):
        pytest.skip("graphrag_mcp .env configuration not found or incomplete")

    # Start server
    cmd = ["uv", "run", str(server_path)]
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=server_path.parent,
    )

    try:
        # Give server time to start
        time.sleep(2)

        # Check if process is running
        assert process.poll() is None, "Server should be running"

        # Try to initialize
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "auto-coder-test", "version": "1.0.0"},
            },
        }

        response = send_jsonrpc_message(process, init_request)

        # Verify we got a response
        assert "id" in response, "Should receive a response with ID"
        assert response["id"] == 1, "Response ID should match"

        logger.info("✅ Successfully connected to graphrag_mcp server")

    finally:
        # Cleanup
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
