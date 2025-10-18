"""MCP configuration checker for various LLM backends.

This module provides utilities to check if graphrag MCP server is configured
for different LLM backends (gemini, qwen, auggie, codex).
"""

import json
from pathlib import Path

from .logger_config import get_logger

logger = get_logger(__name__)


def check_graphrag_mcp_for_backend(backend: str) -> bool:
    """Check if graphrag MCP is configured for the given backend.
    
    Args:
        backend: Backend name (gemini, qwen, auggie, codex, codex-mcp)
        
    Returns:
        True if graphrag MCP is available, False otherwise
    """
    if backend == "gemini":
        return _check_gemini_mcp()
    elif backend == "qwen":
        return _check_qwen_mcp()
    elif backend == "auggie":
        return _check_auggie_mcp()
    elif backend in ("codex", "codex-mcp"):
        return _check_codex_mcp()
    else:
        logger.debug(f"Unknown backend for MCP check: {backend}")
        return False


def _check_gemini_mcp() -> bool:
    """Check if graphrag MCP is configured for Gemini CLI.
    
    Gemini CLI stores MCP configuration in ~/.gemini/config.json
    """
    try:
        config_path = Path.home() / ".gemini" / "config.json"
        if not config_path.exists():
            logger.debug("Gemini config file not found")
            return False
            
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            
        # Check for graphrag in mcpServers
        mcp_servers = config.get("mcpServers", {})
        for server_name in mcp_servers.keys():
            if "graphrag" in server_name.lower():
                logger.info(f"Found graphrag MCP server in Gemini config: {server_name}")
                return True
                
        logger.debug("No graphrag MCP server found in Gemini config")
        return False
    except Exception as e:
        logger.debug(f"Failed to check Gemini MCP config: {e}")
        return False


def _check_qwen_mcp() -> bool:
    """Check if graphrag MCP is configured for Qwen Code CLI.
    
    Qwen Code stores MCP configuration in ~/.qwen/config.toml
    """
    try:
        config_path = Path.home() / ".qwen" / "config.toml"
        if not config_path.exists():
            logger.debug("Qwen config file not found")
            return False
            
        # Read TOML config
        with open(config_path, "r", encoding="utf-8") as f:
            config_content = f.read()
            
        # Simple check for graphrag in config (avoid TOML parser dependency)
        if "graphrag" in config_content.lower():
            logger.info("Found graphrag MCP server in Qwen config")
            return True
            
        logger.debug("No graphrag MCP server found in Qwen config")
        return False
    except Exception as e:
        logger.debug(f"Failed to check Qwen MCP config: {e}")
        return False


def _check_auggie_mcp() -> bool:
    """Check if graphrag MCP is configured for Auggie CLI.
    
    Auggie uses Windsurf/Claude Desktop style configuration.
    Check common locations for MCP configuration.
    """
    try:
        # Check Windsurf config
        windsurf_config = Path.home() / ".windsurf" / "settings.json"
        if windsurf_config.exists():
            with open(windsurf_config, "r", encoding="utf-8") as f:
                config = json.load(f)
            mcp_servers = config.get("mcpServers", {})
            for server_name in mcp_servers.keys():
                if "graphrag" in server_name.lower():
                    logger.info(f"Found graphrag MCP server in Windsurf config: {server_name}")
                    return True
        
        # Check Claude Desktop config
        claude_config = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if claude_config.exists():
            with open(claude_config, "r", encoding="utf-8") as f:
                config = json.load(f)
            mcp_servers = config.get("mcpServers", {})
            for server_name in mcp_servers.keys():
                if "graphrag" in server_name.lower():
                    logger.info(f"Found graphrag MCP server in Claude config: {server_name}")
                    return True
                    
        logger.debug("No graphrag MCP server found in Auggie/Windsurf/Claude config")
        return False
    except Exception as e:
        logger.debug(f"Failed to check Auggie MCP config: {e}")
        return False


def _check_codex_mcp() -> bool:
    """Check if graphrag MCP is configured for Codex CLI.
    
    Codex uses similar configuration to Claude Desktop.
    """
    try:
        # Check Codex config locations
        possible_paths = [
            Path.home() / ".codex" / "config.json",
            Path.home() / ".config" / "codex" / "config.json",
        ]
        
        for config_path in possible_paths:
            if not config_path.exists():
                continue
                
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                
            mcp_servers = config.get("mcpServers", {})
            for server_name in mcp_servers.keys():
                if "graphrag" in server_name.lower():
                    logger.info(f"Found graphrag MCP server in Codex config: {server_name}")
                    return True
                    
        logger.debug("No graphrag MCP server found in Codex config")
        return False
    except Exception as e:
        logger.debug(f"Failed to check Codex MCP config: {e}")
        return False


def suggest_graphrag_mcp_setup(backend: str) -> str:
    """Generate setup instructions for graphrag MCP for the given backend.

    Args:
        backend: Backend name (gemini, qwen, auggie, codex, codex-mcp)

    Returns:
        Setup instructions as a string
    """
    base_setup = """
To enable GraphRAG MCP:

1. Run the automatic setup command:
   auto-coder graphrag setup-mcp

   This will:
   - Clone https://github.com/rileylemm/graphrag_mcp
   - Install dependencies with uv
   - Create .env file with Neo4j and Qdrant configuration
   - Automatically update all backend configuration files

2. Start GraphRAG containers:
   auto-coder graphrag start

"""

    if backend == "gemini":
        return base_setup + """3. Restart Gemini CLI
4. Verify with: gemini (then type /mcp)
"""
    elif backend == "qwen":
        return base_setup + """3. Restart Qwen Code CLI
4. Verify with: qwen mcp list
"""
    elif backend == "auggie":
        return base_setup + """3. Restart Windsurf/Claude application
"""
    elif backend in ("codex", "codex-mcp"):
        return base_setup + """3. Restart Codex CLI
"""
    else:
        return f"No setup instructions available for backend: {backend}"


def add_graphrag_mcp_config(backend: str) -> bool:
    """Add graphrag MCP configuration for the given backend.

    Args:
        backend: Backend name (gemini, qwen, auggie, codex, codex-mcp)

    Returns:
        True if configuration was added successfully, False otherwise
    """
    if backend == "gemini":
        return _add_gemini_mcp_config()
    elif backend == "qwen":
        return _add_qwen_mcp_config()
    elif backend == "auggie":
        return _add_auggie_mcp_config()
    elif backend in ("codex", "codex-mcp"):
        return _add_codex_mcp_config()
    else:
        logger.debug(f"Unknown backend for MCP config addition: {backend}")
        return False


def _add_gemini_mcp_config() -> bool:
    """Add graphrag MCP configuration to Gemini CLI config.

    Note: This function requires setup of GraphRAG MCP server.
    Run 'auto-coder graphrag setup-mcp' to set up automatically.
    """
    logger.warning(
        "GraphRAG MCP requires setup. "
        "Run 'auto-coder graphrag setup-mcp' to set up and configure automatically."
    )
    logger.info(
        "The setup command will:\n"
        "1. Clone https://github.com/rileylemm/graphrag_mcp\n"
        "2. Install dependencies with uv\n"
        "3. Create .env file with Neo4j and Qdrant configuration\n"
        "4. Automatically update ~/.gemini/config.json with GraphRAG MCP configuration"
    )
    return False


def _add_qwen_mcp_config() -> bool:
    """Add graphrag MCP configuration to Qwen Code CLI config.

    Note: This function requires setup of GraphRAG MCP server.
    Run 'auto-coder graphrag setup-mcp' to set up automatically.
    """
    logger.warning(
        "GraphRAG MCP requires setup. "
        "Run 'auto-coder graphrag setup-mcp' to set up and configure automatically."
    )
    logger.info(
        "The setup command will:\n"
        "1. Clone https://github.com/rileylemm/graphrag_mcp\n"
        "2. Install dependencies with uv\n"
        "3. Create .env file with Neo4j and Qdrant configuration\n"
        "4. Automatically update ~/.qwen/config.toml with GraphRAG MCP configuration"
    )
    return False


def _add_auggie_mcp_config() -> bool:
    """Add graphrag MCP configuration to Auggie CLI config (Windsurf).

    Note: This function requires setup of GraphRAG MCP server.
    Run 'auto-coder graphrag setup-mcp' to set up automatically.
    """
    logger.warning(
        "GraphRAG MCP requires setup. "
        "Run 'auto-coder graphrag setup-mcp' to set up and configure automatically."
    )
    logger.info(
        "The setup command will:\n"
        "1. Clone https://github.com/rileylemm/graphrag_mcp\n"
        "2. Install dependencies with uv\n"
        "3. Create .env file with Neo4j and Qdrant configuration\n"
        "4. Automatically update Windsurf settings.json with GraphRAG MCP configuration"
    )
    return False


def _add_codex_mcp_config() -> bool:
    """Add graphrag MCP configuration to Codex CLI config.

    Note: This function requires setup of GraphRAG MCP server.
    Run 'auto-coder graphrag setup-mcp' to set up automatically.
    """
    logger.warning(
        "GraphRAG MCP requires setup. "
        "Run 'auto-coder graphrag setup-mcp' to set up and configure automatically."
    )
    logger.info(
        "The setup command will:\n"
        "1. Clone https://github.com/rileylemm/graphrag_mcp\n"
        "2. Install dependencies with uv\n"
        "3. Create .env file with Neo4j and Qdrant configuration\n"
        "4. Automatically update ~/.codex/config.json with GraphRAG MCP configuration"
    )
    return False


def ensure_graphrag_mcp_configured(backend: str) -> bool:
    """Ensure graphrag MCP is configured for the given backend.

    This function checks if graphrag MCP is configured, and if not,
    automatically adds the configuration.

    Args:
        backend: Backend name (gemini, qwen, auggie, codex, codex-mcp)

    Returns:
        True if graphrag MCP is configured (or was successfully added), False otherwise
    """
    # Check if already configured
    if check_graphrag_mcp_for_backend(backend):
        logger.info(f"GraphRAG MCP server is already configured for {backend}")
        return True

    # Try to add configuration
    logger.info(f"GraphRAG MCP server not found for {backend}. Adding configuration...")
    if add_graphrag_mcp_config(backend):
        # Verify configuration was added
        if check_graphrag_mcp_for_backend(backend):
            logger.info(f"✅ GraphRAG MCP server successfully configured for {backend}")
            return True
        else:
            logger.warning(f"Configuration was added but verification failed for {backend}")
            return False
    else:
        logger.error(f"Failed to add GraphRAG MCP configuration for {backend}")
        logger.info(suggest_graphrag_mcp_setup(backend))
        return False


def check_and_warn_graphrag_mcp(backend: str) -> None:
    """Check graphrag MCP availability and warn if not configured.

    This function is called during LLM client initialization to provide
    early feedback about MCP configuration.

    Args:
        backend: Backend name (gemini, qwen, auggie, codex, codex-mcp)
    """
    if not check_graphrag_mcp_for_backend(backend):
        logger.warning(
            f"GraphRAG MCP server not found for {backend}. "
            f"GraphRAG integration may not be available."
        )
        logger.info(suggest_graphrag_mcp_setup(backend))
    else:
        logger.info(f"GraphRAG MCP server is configured for {backend}")

