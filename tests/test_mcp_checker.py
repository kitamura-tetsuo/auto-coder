"""Tests for MCP configuration checker."""

import json
from pathlib import Path

import pytest

from auto_coder.mcp_checker import add_graphrag_mcp_config, check_graphrag_mcp_for_backend, ensure_graphrag_mcp_configured, suggest_graphrag_mcp_setup


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
                    "command": "/path/to/graphrag_mcp/run_server.sh",
                    "args": [],
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
                    "args": ["run", "--with", "mcp-pdb", "mcp-pdb"],
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
command = "/path/to/graphrag_mcp/run_server.sh"
args = []
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
                    "command": "/path/to/graphrag_mcp/run_server.sh",
                    "args": [],
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
                    "command": "/path/to/graphrag_mcp/run_server.sh",
                    "args": [],
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
        # Check for automatic setup command
        assert "auto-coder graphrag setup-mcp" in suggestion
        assert "Gemini CLI" in suggestion
        assert "Restart Gemini CLI" in suggestion
        assert "gemini" in suggestion

    def test_suggest_graphrag_mcp_setup_qwen(self):
        """Test setup suggestion for Qwen."""
        suggestion = suggest_graphrag_mcp_setup("qwen")
        # Check for automatic setup command
        assert "auto-coder graphrag setup-mcp" in suggestion
        assert "Qwen Code CLI" in suggestion
        assert "qwen mcp list" in suggestion

    def test_suggest_graphrag_mcp_setup_auggie(self):
        """Test setup suggestion for Auggie."""
        suggestion = suggest_graphrag_mcp_setup("auggie")
        # Check for automatic setup command
        assert "auto-coder graphrag setup-mcp" in suggestion
        assert "Windsurf" in suggestion or "Claude" in suggestion

    def test_suggest_graphrag_mcp_setup_codex(self):
        """Test setup suggestion for Codex."""
        suggestion = suggest_graphrag_mcp_setup("codex")
        # Check for automatic setup command
        assert "auto-coder graphrag setup-mcp" in suggestion
        assert "Codex CLI" in suggestion
        assert "Restart Codex CLI" in suggestion

    def test_suggest_graphrag_mcp_setup_unknown(self):
        """Test setup suggestion for unknown backend."""
        suggestion = suggest_graphrag_mcp_setup("unknown")
        assert "No setup instructions available" in suggestion

    def test_add_gemini_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Gemini MCP configuration returns False (manual setup required)."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration - should return False because manual setup is required
        result = add_graphrag_mcp_config("gemini")
        assert result is False

    def test_add_qwen_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Qwen MCP configuration returns False (manual setup required)."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration - should return False because manual setup is required
        result = add_graphrag_mcp_config("qwen")
        assert result is False

    def test_add_auggie_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Auggie MCP configuration returns False (manual setup required)."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration - should return False because manual setup is required
        result = add_graphrag_mcp_config("auggie")
        assert result is False

    def test_add_codex_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Codex MCP configuration returns False (manual setup required)."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Add configuration - should return False because manual setup is required
        result = add_graphrag_mcp_config("codex")
        assert result is False

    def test_ensure_graphrag_mcp_configured_adds_config(self, tmp_path, monkeypatch):
        """Test ensure_graphrag_mcp_configured returns False when not present (manual setup required)."""
        # Point home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Ensure configuration - should return False because manual setup is required
        result = ensure_graphrag_mcp_configured("gemini")
        assert result is False

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
                    "command": "/path/to/graphrag_mcp/run_server.sh",
                    "args": [],
                }
            }
        }

        with open(config_file, "w") as f:
            json.dump(config, f)

        # Ensure configuration (should detect existing)
        result = ensure_graphrag_mcp_configured("gemini")
        assert result is True

    def test_check_claude_mcp_not_configured(self, tmp_path, monkeypatch):
        """Test Claude MCP check when config file doesn't exist."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = check_graphrag_mcp_for_backend("claude")
        assert result is False

    def test_check_claude_mcp_configured(self, tmp_path, monkeypatch):
        """Test Claude MCP check when graphrag is configured."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".claude"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        config = {"mcpServers": {"graphrag": {"command": "/path/run_server.sh", "args": []}}}
        with open(config_file, "w") as f:
            json.dump(config, f)
        result = check_graphrag_mcp_for_backend("claude")
        assert result is True

    def test_suggest_graphrag_mcp_setup_claude(self):
        """Test setup suggestion for Claude."""
        suggestion = suggest_graphrag_mcp_setup("claude")
        assert "auto-coder graphrag setup-mcp" in suggestion
        assert "Claude CLI" in suggestion
        assert "claude mcp" in suggestion

    def test_add_claude_mcp_config(self, tmp_path, monkeypatch):
        """Test adding Claude MCP configuration returns False (manual setup required)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = add_graphrag_mcp_config("claude")
        assert result is False

    def test_ensure_graphrag_mcp_configured_claude_already_configured(self, tmp_path, monkeypatch):
        """Test ensure_graphrag_mcp_configured when Claude already configured."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".claude"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        config = {"mcpServers": {"graphrag": {"command": "/path/run_server.sh", "args": []}}}
        with open(config_file, "w") as f:
            json.dump(config, f)
        result = ensure_graphrag_mcp_configured("claude", auto_setup=False)
        assert result is True
