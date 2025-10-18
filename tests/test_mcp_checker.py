"""Tests for MCP configuration checker."""

import json
from pathlib import Path

import pytest

from src.auto_coder.mcp_checker import (
    add_graphrag_mcp_config,
    check_graphrag_mcp_for_backend,
    ensure_graphrag_mcp_configured,
    suggest_graphrag_mcp_setup,
)


class TestMCPChecker:
    """Test MCP configuration checker functionality."""

    def test_check_gemini_mcp_not_configured(self, tmp_path, monkeypatch):
        """Test Gemini MCP check when config file doesn't exist."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        
        result = check_graphrag_mcp_for_backend("gemini")
        assert result is False

    def test_check_gemini_mcp_configured(self, tmp_path, monkeypatch):
        """Test Gemini MCP check when graphrag is configured."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        
        # Create config directory and file
        config_dir = tmp_path / ".gemini"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        
        config = {
            "mcpServers": {
                "graphrag": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-graphrag"]
                }
            }
        }
        
        with open(config_file, "w") as f:
            json.dump(config, f)
        
        result = check_graphrag_mcp_for_backend("gemini")
        assert result is True

    def test_check_gemini_mcp_other_servers_only(self, tmp_path, monkeypatch):
        """Test Gemini MCP check when only other servers are configured."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        
        # Create config directory and file
        config_dir = tmp_path / ".gemini"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        
        config = {
            "mcpServers": {
                "mcp-pdb": {
                    "command": "uv",
                    "args": ["run", "--with", "mcp-pdb", "mcp-pdb"]
                }
            }
        }
        
        with open(config_file, "w") as f:
            json.dump(config, f)
        
        result = check_graphrag_mcp_for_backend("gemini")
        assert result is False

    def test_check_qwen_mcp_not_configured(self, tmp_path, monkeypatch):
        """Test Qwen MCP check when config file doesn't exist."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        
        result = check_graphrag_mcp_for_backend("qwen")
        assert result is False

    def test_check_qwen_mcp_configured(self, tmp_path, monkeypatch):
        """Test Qwen MCP check when graphrag is configured."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        
        # Create config directory and file
        config_dir = tmp_path / ".qwen"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        
        config_content = """
[mcp_servers.graphrag]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-graphrag"]
"""
        
        with open(config_file, "w") as f:
            f.write(config_content)
        
        result = check_graphrag_mcp_for_backend("qwen")
        assert result is True

    def test_check_auggie_mcp_windsurf_configured(self, tmp_path, monkeypatch):
        """Test Auggie MCP check when graphrag is configured in Windsurf."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        
        # Create Windsurf config directory and file
        config_dir = tmp_path / ".windsurf"
        config_dir.mkdir()
        config_file = config_dir / "settings.json"
        
        config = {
            "mcpServers": {
                "graphrag": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-graphrag"]
                }
            }
        }
        
        with open(config_file, "w") as f:
            json.dump(config, f)
        
        result = check_graphrag_mcp_for_backend("auggie")
        assert result is True

    def test_check_codex_mcp_configured(self, tmp_path, monkeypatch):
        """Test Codex MCP check when graphrag is configured."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        
        # Create config directory and file
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        
        config = {
            "mcpServers": {
                "graphrag": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-graphrag"]
                }
            }
        }
        
        with open(config_file, "w") as f:
            json.dump(config, f)
        
        result = check_graphrag_mcp_for_backend("codex")
        assert result is True

    def test_check_unknown_backend(self):
        """Test MCP check for unknown backend."""
        result = check_graphrag_mcp_for_backend("unknown")
        assert result is False

    def test_suggest_graphrag_mcp_setup_gemini(self):
        """Test setup suggestion for Gemini."""
        suggestion = suggest_graphrag_mcp_setup("gemini")
        assert "Gemini CLI" in suggestion
        assert "~/.gemini/config.json" in suggestion
        assert "mcpServers" in suggestion

    def test_suggest_graphrag_mcp_setup_qwen(self):
        """Test setup suggestion for Qwen."""
        suggestion = suggest_graphrag_mcp_setup("qwen")
        assert "Qwen Code CLI" in suggestion
        assert "~/.qwen/config.toml" in suggestion
        assert "mcp_servers.graphrag" in suggestion

    def test_suggest_graphrag_mcp_setup_auggie(self):
        """Test setup suggestion for Auggie."""
        suggestion = suggest_graphrag_mcp_setup("auggie")
        assert "Auggie CLI" in suggestion
        assert "Windsurf" in suggestion or "Claude Desktop" in suggestion

    def test_suggest_graphrag_mcp_setup_codex(self):
        """Test setup suggestion for Codex."""
        suggestion = suggest_graphrag_mcp_setup("codex")
        assert "Codex CLI" in suggestion
        assert "~/.codex/config.json" in suggestion

    def test_suggest_graphrag_mcp_setup_unknown(self):
        """Test setup suggestion for unknown backend."""
        suggestion = suggest_graphrag_mcp_setup("unknown")
        assert "No setup instructions available" in suggestion

    def test_add_gemini_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Gemini MCP configuration."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration
        result = add_graphrag_mcp_config("gemini")
        assert result is True

        # Verify configuration was added
        config_file = tmp_path / ".gemini" / "config.json"
        assert config_file.exists()

        with open(config_file, "r") as f:
            config = json.load(f)

        assert "mcpServers" in config
        assert "graphrag" in config["mcpServers"]
        assert config["mcpServers"]["graphrag"]["command"] == "npx"

    def test_add_qwen_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Qwen MCP configuration."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration
        result = add_graphrag_mcp_config("qwen")
        assert result is True

        # Verify configuration was added
        config_file = tmp_path / ".qwen" / "config.toml"
        assert config_file.exists()

        with open(config_file, "r") as f:
            config_content = f.read()

        assert "graphrag" in config_content.lower()
        assert "npx" in config_content

    def test_add_auggie_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Auggie MCP configuration."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration
        result = add_graphrag_mcp_config("auggie")
        assert result is True

        # Verify configuration was added
        config_file = tmp_path / ".windsurf" / "settings.json"
        assert config_file.exists()

        with open(config_file, "r") as f:
            config = json.load(f)

        assert "mcpServers" in config
        assert "graphrag" in config["mcpServers"]

    def test_add_codex_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Codex MCP configuration."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration
        result = add_graphrag_mcp_config("codex")
        assert result is True

        # Verify configuration was added
        config_file = tmp_path / ".codex" / "config.json"
        assert config_file.exists()

        with open(config_file, "r") as f:
            config = json.load(f)

        assert "mcpServers" in config
        assert "graphrag" in config["mcpServers"]

    def test_ensure_graphrag_mcp_configured_adds_config(self, tmp_path, monkeypatch):
        """Test ensure_graphrag_mcp_configured adds configuration when not present."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Ensure configuration
        result = ensure_graphrag_mcp_configured("gemini")
        assert result is True

        # Verify configuration was added
        config_file = tmp_path / ".gemini" / "config.json"
        assert config_file.exists()

    def test_ensure_graphrag_mcp_configured_already_configured(self, tmp_path, monkeypatch):
        """Test ensure_graphrag_mcp_configured when already configured."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Create config directory and file
        config_dir = tmp_path / ".gemini"
        config_dir.mkdir()
        config_file = config_dir / "config.json"

        config = {
            "mcpServers": {
                "graphrag": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-graphrag"]
                }
            }
        }

        with open(config_file, "w") as f:
            json.dump(config, f)

        # Ensure configuration (should detect existing)
        result = ensure_graphrag_mcp_configured("gemini")
        assert result is True

