"""Tests for LLM client MCP configuration methods."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.auggie_client import AuggieClient
from auto_coder.backend_manager import BackendManager
from auto_coder.codex_client import CodexClient
from auto_coder.codex_mcp_client import CodexMCPClient
from auto_coder.gemini_client import GeminiClient
from auto_coder.qwen_client import QwenClient


class TestGeminiClientMCP(unittest.TestCase):
    """Test Gemini client MCP configuration methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.temp_dir) / ".gemini"
        self.config_path = self.config_dir / "config.json"

    def test_check_mcp_server_not_configured(self):
        """Test checking for MCP server when not configured."""
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = GeminiClient()
            result = client.check_mcp_server_configured("graphrag")
            self.assertFalse(result)

    @patch("subprocess.run")
    def test_check_mcp_server_configured(self, mock_run):
        """Test checking for MCP server when configured."""

        # Mock subprocess.run to return success for --version and mcp list with graphrag
        def mock_run_side_effect(cmd, **kwargs):
            if "--version" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                return mock_result
            elif "mcp" in cmd and "list" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = "graphrag\nother-server\n"
                return mock_result
            else:
                raise FileNotFoundError()

        mock_run.side_effect = mock_run_side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = GeminiClient()
            result = client.check_mcp_server_configured("graphrag")
            self.assertTrue(result)

    @patch("subprocess.run")
    def test_add_mcp_server_config(self, mock_run):
        """Test adding MCP server configuration."""

        # Mock subprocess.run to return success for --version and mcp add
        def mock_run_side_effect(cmd, **kwargs):
            if "--version" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                return mock_result
            elif "mcp" in cmd and "add" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = "MCP server 'graphrag' added successfully"
                return mock_result
            else:
                raise FileNotFoundError()

        mock_run.side_effect = mock_run_side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = GeminiClient()
            result = client.add_mcp_server_config("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)

            # Verify gemini mcp add was called with correct arguments
            add_call = None
            for call in mock_run.call_args_list:
                if len(call[0]) > 0 and "mcp" in call[0][0] and "add" in call[0][0]:
                    add_call = call
                    break

            self.assertIsNotNone(add_call)
            cmd = add_call[0][0]
            self.assertEqual(cmd[0], "gemini")
            self.assertEqual(cmd[1], "mcp")
            self.assertEqual(cmd[2], "add")
            self.assertEqual(cmd[3], "mcp-pdb")
            self.assertEqual(cmd[4], "uv")
            self.assertIn("run", cmd)
            self.assertIn("mcp-pdb", cmd)

    @patch("subprocess.run")
    def test_ensure_mcp_server_configured(self, mock_run):
        """Test ensuring MCP server is configured."""
        call_count = {"mcp_list": 0}

        # Mock subprocess.run to return success for --version, mcp add, and mcp list
        def mock_run_side_effect(cmd, **kwargs):
            if "--version" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                return mock_result
            elif "mcp" in cmd and "add" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = "MCP server 'mcp-pdb' added successfully"
                return mock_result
            elif "mcp" in cmd and "list" in cmd:
                call_count["mcp_list"] += 1
                mock_result = MagicMock()
                mock_result.returncode = 0
                # First call: not configured, second call: configured
                if call_count["mcp_list"] == 1:
                    mock_result.stdout = "other-server\n"
                else:
                    mock_result.stdout = "mcp-pdb\nother-server\n"
                return mock_result
            else:
                raise FileNotFoundError()

        mock_run.side_effect = mock_run_side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = GeminiClient()
            result = client.ensure_mcp_server_configured("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)

            # Verify it's now configured
            result2 = client.check_mcp_server_configured("mcp-pdb")
            self.assertTrue(result2)


class TestQwenClientMCP(unittest.TestCase):
    """Test Qwen client MCP configuration methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.temp_dir) / ".qwen"
        self.config_path = self.config_dir / "config.toml"

    @patch("subprocess.run")
    def test_check_mcp_server_not_configured(self, mock_run):
        """Test checking for MCP server when not configured."""
        # Mock qwen mcp list to return no servers
        mock_run.return_value = MagicMock(returncode=0, stdout="No MCP servers configured.\n")

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = QwenClient()
            result = client.check_mcp_server_configured("mcp-pdb")
            self.assertFalse(result)

    @patch("subprocess.run")
    def test_add_mcp_server_config(self, mock_run):
        """Test adding MCP server configuration."""
        call_count = {"check": 0, "add": 0}

        def mock_run_side_effect(cmd, **kwargs):
            if "mcp" in cmd and "list" in cmd:
                call_count["check"] += 1
                mock_result = MagicMock()
                mock_result.returncode = 0
                # First call: not configured, second call: configured
                if call_count["check"] == 1:
                    mock_result.stdout = "No MCP servers configured.\n"
                else:
                    mock_result.stdout = "✓ mcp-pdb: uv run mcp-pdb (stdio) - Connected\n"
                return mock_result
            elif "mcp" in cmd and "add" in cmd:
                call_count["add"] += 1
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = 'MCP server "mcp-pdb" added to user settings. (stdio)\n'
                return mock_result
            elif "--version" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                return mock_result
            else:
                raise FileNotFoundError()

        mock_run.side_effect = mock_run_side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = QwenClient()
            result = client.add_mcp_server_config("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)

            # Verify qwen mcp add was called
            self.assertEqual(call_count["add"], 1)

    @patch("subprocess.run")
    def test_ensure_mcp_server_configured(self, mock_run):
        """Test ensuring MCP server is configured."""
        call_count = {"check": 0, "add": 0, "version": 0}

        def mock_run_side_effect(cmd, **kwargs):
            if "--version" in cmd:
                call_count["version"] += 1
                mock_result = MagicMock()
                mock_result.returncode = 0
                return mock_result
            elif "mcp" in cmd and "list" in cmd:
                call_count["check"] += 1
                mock_result = MagicMock()
                mock_result.returncode = 0
                # First call: not configured, second call: configured
                if call_count["check"] == 1:
                    mock_result.stdout = "No MCP servers configured.\n"
                else:
                    mock_result.stdout = "✓ mcp-pdb: uv run mcp-pdb (stdio) - Connected\n"
                return mock_result
            elif "mcp" in cmd and "add" in cmd:
                call_count["add"] += 1
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = 'MCP server "mcp-pdb" added to user settings. (stdio)\n'
                return mock_result
            else:
                raise FileNotFoundError()

        mock_run.side_effect = mock_run_side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = QwenClient()
            result = client.ensure_mcp_server_configured("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)

            # Verify qwen mcp add was called
            self.assertEqual(call_count["add"], 1)
            # Verify check was called twice (before and after add)
            self.assertEqual(call_count["check"], 2)


class TestAuggieClientMCP(unittest.TestCase):
    """Test Auggie client MCP configuration methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.temp_dir) / ".windsurf"
        self.config_path = self.config_dir / "settings.json"

    @patch("subprocess.run")
    def test_check_mcp_server_not_configured(self, mock_run):
        """Test checking for MCP server when not configured."""
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0")
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = AuggieClient()
            result = client.check_mcp_server_configured("graphrag")
            self.assertFalse(result)

    @patch("subprocess.run")
    def test_add_mcp_server_config(self, mock_run):
        """Test adding MCP server configuration."""

        # Mock subprocess.run to return success for --version and mcp add
        def mock_run_side_effect(cmd, **kwargs):
            if "--version" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = "1.0.0"
                return mock_result
            elif "mcp" in cmd and "add" in cmd:
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = 'MCP server "graphrag" added successfully'
                return mock_result
            else:
                raise FileNotFoundError()

        mock_run.side_effect = mock_run_side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = AuggieClient()
            result = client.add_mcp_server_config("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)

            # Verify auggie mcp add was called with correct arguments
            add_call = None
            for call in mock_run.call_args_list:
                if len(call[0]) > 0 and "mcp" in call[0][0] and "add" in call[0][0]:
                    add_call = call
                    break

            self.assertIsNotNone(add_call)
            cmd = add_call[0][0]
            self.assertEqual(cmd[0], "auggie")
            self.assertEqual(cmd[1], "mcp")
            self.assertEqual(cmd[2], "add")
            self.assertEqual(cmd[3], "mcp-pdb")
            self.assertIn("--command", cmd)
            self.assertIn("uv", cmd)
            self.assertIn("--args", cmd)


class TestCodexClientMCP(unittest.TestCase):
    """Test Codex client MCP configuration methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.temp_dir) / ".codex"
        self.config_path = self.config_dir / "config.json"

    @patch("subprocess.run")
    def test_check_mcp_server_not_configured(self, mock_run):
        """Test checking for MCP server when not configured."""
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0")
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = CodexClient()
            result = client.check_mcp_server_configured("graphrag")
            self.assertFalse(result)

    @patch("subprocess.run")
    def test_add_mcp_server_config(self, mock_run):
        """Test adding MCP server configuration."""
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0")
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = CodexClient()
            result = client.add_mcp_server_config("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)

            # Verify config was written
            self.assertTrue(self.config_path.exists())
            with open(self.config_path, "r") as f:
                config = json.load(f)
            self.assertIn("mcpServers", config)
            self.assertIn("mcp-pdb", config["mcpServers"])


class TestCodexMCPClientMCP(unittest.TestCase):
    """Test CodexMCP client MCP configuration methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.temp_dir) / ".codex"
        self.config_path = self.config_dir / "config.json"

    @patch("subprocess.Popen")
    def test_check_mcp_server_not_configured(self, mock_popen):
        """Test checking for MCP server when not configured."""
        # Mock the MCP process
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = CodexMCPClient()
            result = client.check_mcp_server_configured("graphrag")
            self.assertFalse(result)
            client.close()

    @patch("subprocess.Popen")
    def test_add_mcp_server_config(self, mock_popen):
        """Test adding MCP server configuration."""
        # Mock the MCP process
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            client = CodexMCPClient()
            result = client.add_mcp_server_config("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)

            # Verify config was written
            self.assertTrue(self.config_path.exists())
            with open(self.config_path, "r") as f:
                config = json.load(f)
            self.assertIn("mcpServers", config)
            self.assertIn("mcp-pdb", config["mcpServers"])
            client.close()


class TestBackendManagerMCP(unittest.TestCase):
    """Test BackendManager MCP configuration methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()

    def test_check_mcp_server_configured(self):
        """Test checking MCP server through BackendManager."""
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            # Create a mock client
            mock_client = MagicMock()
            mock_client.check_mcp_server_configured.return_value = True

            # Create BackendManager with mock client
            manager = BackendManager(
                default_backend="gemini",
                default_client=mock_client,
                factories={},
                order=["gemini"],
            )

            result = manager.check_mcp_server_configured("graphrag")
            self.assertTrue(result)
            mock_client.check_mcp_server_configured.assert_called_once_with("graphrag")

    def test_add_mcp_server_config(self):
        """Test adding MCP server config through BackendManager."""
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            # Create a mock client
            mock_client = MagicMock()
            mock_client.add_mcp_server_config.return_value = True

            # Create BackendManager with mock client
            manager = BackendManager(
                default_backend="gemini",
                default_client=mock_client,
                factories={},
                order=["gemini"],
            )

            result = manager.add_mcp_server_config("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(result)
            mock_client.add_mcp_server_config.assert_called_once_with("mcp-pdb", "uv", ["run", "mcp-pdb"])

    def test_ensure_mcp_server_configured_all_backends(self):
        """Test ensuring MCP server is configured for all backends."""
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            # Create mock clients for multiple backends
            mock_gemini = MagicMock()
            mock_gemini.ensure_mcp_server_configured.return_value = True

            mock_qwen = MagicMock()
            mock_qwen.ensure_mcp_server_configured.return_value = True

            mock_codex = MagicMock()
            mock_codex.ensure_mcp_server_configured.return_value = True

            # Create factories - note that these return the SAME mock instances
            # This is important because BackendManager caches clients
            def factory_gemini():
                return mock_gemini

            def factory_qwen():
                return mock_qwen

            def factory_codex():
                return mock_codex

            # Create BackendManager with multiple backends
            # Note: we pass mock_gemini as default_client, so it won't use the factory for gemini
            manager = BackendManager(
                default_backend="gemini",
                default_client=mock_gemini,
                factories={
                    "gemini": factory_gemini,
                    "qwen": factory_qwen,
                    "codex": factory_codex,
                },
                order=["gemini", "qwen", "codex"],
            )

            # Ensure MCP server is configured for all backends
            result = manager.ensure_mcp_server_configured("mcp-pdb", "uv", ["run", "mcp-pdb"])

            # Should succeed for all backends
            self.assertTrue(result)

            # Verify ensure_mcp_server_configured was called for all backends
            mock_gemini.ensure_mcp_server_configured.assert_called_once_with("mcp-pdb", "uv", ["run", "mcp-pdb"])
            mock_qwen.ensure_mcp_server_configured.assert_called_once_with("mcp-pdb", "uv", ["run", "mcp-pdb"])
            mock_codex.ensure_mcp_server_configured.assert_called_once_with("mcp-pdb", "uv", ["run", "mcp-pdb"])

    def test_ensure_mcp_server_configured_partial_failure(self):
        """Test ensuring MCP server when one backend fails."""
        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            # Create mock clients
            mock_gemini = MagicMock()
            mock_gemini.ensure_mcp_server_configured.return_value = False  # Fails

            mock_qwen = MagicMock()
            mock_qwen.ensure_mcp_server_configured.return_value = True  # Succeeds

            # Create factory
            def factory_qwen():
                return mock_qwen

            # Create BackendManager
            manager = BackendManager(
                default_backend="gemini",
                default_client=mock_gemini,
                factories={"qwen": factory_qwen},
                order=["gemini", "qwen"],
            )

            # Ensure MCP server is configured
            result = manager.ensure_mcp_server_configured("mcp-pdb", "uv", ["run", "mcp-pdb"])

            # Should fail because gemini failed
            self.assertFalse(result)

            # Verify both backends were attempted
            mock_gemini.ensure_mcp_server_configured.assert_called_once_with("mcp-pdb", "uv", ["run", "mcp-pdb"])
            mock_qwen.ensure_mcp_server_configured.assert_called_once_with("mcp-pdb", "uv", ["run", "mcp-pdb"])


if __name__ == "__main__":
    unittest.main()


class TestClaudeClientMCP(unittest.TestCase):
    """Test Claude client MCP configuration methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.temp_dir) / ".claude"
        self.config_path = self.config_dir / "config.json"

    @patch("subprocess.run")
    def test_check_mcp_server_not_configured(self, mock_run):
        """Test checking for MCP server when not configured."""

        # First call from __init__: 'claude --version'
        # Second call from check: 'claude mcp'
        def side_effect(cmd, **kwargs):
            if "--version" in cmd:
                m = MagicMock()
                m.returncode = 0
                m.stdout = "1.0.0"
                return m
            elif len(cmd) >= 2 and cmd[0] == "claude" and cmd[1] == "mcp":
                m = MagicMock()
                m.returncode = 0
                m.stdout = "other-server\n"
                return m
            raise FileNotFoundError()

        mock_run.side_effect = side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient()
            result = client.check_mcp_server_configured("graphrag")
            self.assertFalse(result)

    @patch("subprocess.run")
    def test_check_mcp_server_configured(self, mock_run):
        """Test checking for MCP server when configured."""

        def side_effect(cmd, **kwargs):
            if "--version" in cmd:
                m = MagicMock()
                m.returncode = 0
                m.stdout = "1.0.0"
                return m
            elif len(cmd) >= 2 and cmd[0] == "claude" and cmd[1] == "mcp":
                m = MagicMock()
                m.returncode = 0
                m.stdout = "graphrag\n"
                return m
            raise FileNotFoundError()

        mock_run.side_effect = side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient()
            self.assertTrue(client.check_mcp_server_configured("graphrag"))

    @patch("subprocess.run")
    def test_add_mcp_server_config(self, mock_run):
        """Test adding MCP server configuration writes to ~/.claude/config.json."""
        # Only __init__ uses subprocess.run here
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0")

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient()
            ok = client.add_mcp_server_config("mcp-pdb", "uv", ["run", "mcp-pdb"])
            self.assertTrue(ok)
            # Verify config file created and contains the server
            self.assertTrue(self.config_path.exists())
            with open(self.config_path, "r") as f:
                cfg = json.load(f)
            self.assertIn("mcpServers", cfg)
            self.assertIn("mcp-pdb", cfg["mcpServers"])

    @patch("subprocess.run")
    def test_ensure_mcp_server_configured(self, mock_run):
        """Test ensuring MCP server is configured for Claude."""
        calls = {"mcp": 0}

        def side_effect(cmd, **kwargs):
            if "--version" in cmd:
                m = MagicMock()
                m.returncode = 0
                m.stdout = "1.0.0"
                return m
            if len(cmd) >= 2 and cmd[0] == "claude" and cmd[1] == "mcp":
                calls["mcp"] += 1
                m = MagicMock()
                m.returncode = 0
                # first check -> not configured, second check -> configured
                m.stdout = "other\n" if calls["mcp"] == 1 else "graphrag\n"
                return m
            raise FileNotFoundError()

        mock_run.side_effect = side_effect

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient()
            ok = client.ensure_mcp_server_configured("graphrag", "uv", ["run", "main.py"])
            self.assertTrue(ok)
