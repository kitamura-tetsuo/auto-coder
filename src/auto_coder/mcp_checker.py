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
    if backend == "gemini":
        return """
To enable GraphRAG MCP for Gemini CLI:

1. Edit ~/.gemini/config.json and add:
   {
     "mcpServers": {
       "graphrag": {
         "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-graphrag"]
       }
     }
   }

2. Restart Gemini CLI
3. Verify with: gemini (then type /mcp)
"""
    elif backend == "qwen":
        return """
To enable GraphRAG MCP for Qwen Code CLI:

1. Edit ~/.qwen/config.toml and add:
   [mcp_servers.graphrag]
   command = "npx"
   args = ["-y", "@modelcontextprotocol/server-graphrag"]

2. Restart Qwen Code CLI
3. Verify with: qwen --mcp-status
"""
    elif backend == "auggie":
        return """
To enable GraphRAG MCP for Auggie CLI:

1. For Windsurf, edit ~/.windsurf/settings.json and add:
   {
     "mcpServers": {
       "graphrag": {
         "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-graphrag"]
       }
     }
   }

2. For Claude Desktop, run:
   claude mcp add graphrag -- npx -y @modelcontextprotocol/server-graphrag

3. Restart the application
"""
    elif backend in ("codex", "codex-mcp"):
        return """
To enable GraphRAG MCP for Codex CLI:

1. Edit ~/.codex/config.json and add:
   {
     "mcpServers": {
       "graphrag": {
         "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-graphrag"]
       }
     }
   }

2. Restart Codex CLI
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
    """Add graphrag MCP configuration to Gemini CLI config."""
    try:
        config_dir = Path.home() / ".gemini"
        config_path = config_dir / "config.json"

        # Create directory if it doesn't exist
        config_dir.mkdir(parents=True, exist_ok=True)

        # Read existing config or create new one
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {}

        # Add graphrag MCP server
        if "mcpServers" not in config:
            config["mcpServers"] = {}

        config["mcpServers"]["graphrag"] = {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-graphrag"]
        }

        # Write config
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        logger.info(f"Added graphrag MCP configuration to {config_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to add Gemini MCP config: {e}")
        return False


def _add_qwen_mcp_config() -> bool:
    """Add graphrag MCP configuration to Qwen Code CLI config."""
    try:
        config_dir = Path.home() / ".qwen"
        config_path = config_dir / "config.toml"

        # Create directory if it doesn't exist
        config_dir.mkdir(parents=True, exist_ok=True)

        # Read existing config or create new one
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config_content = f.read()
        else:
            config_content = ""

        # Check if graphrag is already configured
        if "graphrag" in config_content.lower():
            logger.info("GraphRAG MCP already configured in Qwen config")
            return True

        # Add graphrag MCP server configuration
        graphrag_config = """
[mcp_servers.graphrag]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-graphrag"]
"""

        # Append to existing config
        if config_content and not config_content.endswith("\n"):
            config_content += "\n"
        config_content += graphrag_config

        # Write config
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_content)

        logger.info(f"Added graphrag MCP configuration to {config_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to add Qwen MCP config: {e}")
        return False


def _add_auggie_mcp_config() -> bool:
    """Add graphrag MCP configuration to Auggie CLI config (Windsurf)."""
    try:
        # Try Windsurf config first
        config_dir = Path.home() / ".windsurf"
        config_path = config_dir / "settings.json"

        # Create directory if it doesn't exist
        config_dir.mkdir(parents=True, exist_ok=True)

        # Read existing config or create new one
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {}

        # Add graphrag MCP server
        if "mcpServers" not in config:
            config["mcpServers"] = {}

        config["mcpServers"]["graphrag"] = {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-graphrag"]
        }

        # Write config
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        logger.info(f"Added graphrag MCP configuration to {config_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to add Auggie MCP config: {e}")
        return False


def _add_codex_mcp_config() -> bool:
    """Add graphrag MCP configuration to Codex CLI config."""
    try:
        config_dir = Path.home() / ".codex"
        config_path = config_dir / "config.json"

        # Create directory if it doesn't exist
        config_dir.mkdir(parents=True, exist_ok=True)

        # Read existing config or create new one
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {}

        # Add graphrag MCP server
        if "mcpServers" not in config:
            config["mcpServers"] = {}

        config["mcpServers"]["graphrag"] = {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-graphrag"]
        }

        # Write config
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        logger.info(f"Added graphrag MCP configuration to {config_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to add Codex MCP config: {e}")
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

