"""
Tests for the 'config' CLI commands.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from src.auto_coder.cli import main
from src.auto_coder.llm_backend_config import LLMBackendConfiguration


def test_config_group_help():
    """Test the help output for the 'config' command group."""
    runner = CliRunner()
    result = runner.invoke(main, ["config", "--help"])
    assert result.exit_code == 0
    assert "Configuration management commands." in result.output
    assert "show" in result.output
    assert "edit" in result.output
    assert "set" in result.output
    assert "get" in result.output
    assert "reset" in result.output
    assert "validate" in result.output


def test_config_show(tmp_path: Path):
    """Test the 'config show' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Test with no existing file
    result = runner.invoke(main, ["config", "show", "--file", str(config_file)])
    assert result.exit_code == 0
    config_data = json.loads(result.output)
    assert "codex" in config_data["backends"]
    assert "gemini" in config_data["backends"]

    # Test with an existing file
    config = LLMBackendConfiguration()
    config.get_backend_config("gemini").model = "gemini-pro-test"
    config.save_to_file(config_file)

    result = runner.invoke(main, ["config", "show", "--file", str(config_file)])
    assert result.exit_code == 0
    config_data = json.loads(result.output)
    assert config_data["backends"]["gemini"]["model"] == "gemini-pro-test"


def test_config_set_and_get(tmp_path: Path):
    """Test the 'config set' and 'config get' commands."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Set a value
    result = runner.invoke(
        main,
        [
            "config",
            "set",
            "--file",
            str(config_file),
            "gemini.model",
            "gemini-ultra-test",
        ],
    )
    assert result.exit_code == 0
    assert "Set gemini.model = gemini-ultra-test" in result.output

    # Get the value
    result = runner.invoke(main, ["config", "get", "--file", str(config_file), "gemini.model"])
    assert result.exit_code == 0
    assert "gemini-ultra-test" in result.output.strip()

    # Test setting a float
    result = runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.temperature", "0.8"])
    assert result.exit_code == 0

    result = runner.invoke(main, ["config", "get", "--file", str(config_file), "gemini.temperature"])
    assert result.exit_code == 0
    assert "0.8" in result.output


def test_config_reset(tmp_path: Path):
    """Test the 'config reset' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Set a value to change it from default
    runner.invoke(
        main,
        [
            "config",
            "set",
            "--file",
            str(config_file),
            "gemini.model",
            "gemini-ultra-test",
        ],
    )

    # Reset the config
    result = runner.invoke(main, ["config", "reset", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration reset to default values" in result.output

    # Check if the value is reset
    result = runner.invoke(main, ["config", "get", "--file", str(config_file), "gemini.model"])
    assert result.exit_code == 0
    assert result.output == "\n"


@patch("subprocess.run")
def test_config_edit(mock_subprocess_run: MagicMock, tmp_path: Path):
    """Test the 'config edit' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Mock the editor
    editor = os.environ.get("EDITOR", "nano")

    # Test with no existing file
    result = runner.invoke(main, ["config", "edit", "--file", str(config_file)])
    assert result.exit_code == 0
    mock_subprocess_run.assert_called_with([editor, str(config_file)], check=True)
    assert config_file.exists()

    # Test with existing file
    mock_subprocess_run.reset_mock()
    result = runner.invoke(main, ["config", "edit", "--file", str(config_file)])
    assert result.exit_code == 0
    mock_subprocess_run.assert_called_with([editor, str(config_file)], check=True)


def test_config_validate(tmp_path: Path):
    """Test the 'config validate' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Test with no file
    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration file does not exist" in result.output

    # Test with a valid file
    config = LLMBackendConfiguration()
    # Add required options for each backend to make it valid
    codex_backend = config.get_backend_config("codex")
    if codex_backend:
        codex_backend.options = ["--dangerously-bypass-approvals-and-sandbox"]

    gemini_backend = config.get_backend_config("gemini")
    if gemini_backend:
        gemini_backend.options = ["--yolo"]

    qwen_backend = config.get_backend_config("qwen")
    if qwen_backend:
        qwen_backend.options = ["-y"]

    auggie_backend = config.get_backend_config("auggie")
    if auggie_backend:
        auggie_backend.options = ["--print"]

    claude_backend = config.get_backend_config("claude")
    if claude_backend:
        claude_backend.options = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]

    config.save_to_file(config_file)
    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration is valid" in result.output

    # Test with missing required options
    config2 = LLMBackendConfiguration()
    config2.save_to_file(config_file)
    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 1
    assert "Configuration validation errors found" in result.output
    assert "missing required option" in result.output
    assert "codex" in result.output
    assert "gemini" in result.output

    # Test with an invalid file
    with open(config_file, "w") as f:
        f.write("[backends.gemini]\nmodel = 123")  # model should be a string

    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 1
    assert "Configuration validation errors found" in result.output
    assert "gemini.model must be a string" in result.output
