"""
Tests for the 'config' CLI commands.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from auto_coder.cli import main
from auto_coder.llm_backend_config import LLMBackendConfiguration


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
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "gemini-pro-test"
    config.save_to_file(str(config_file))

    result = runner.invoke(main, ["config", "show", "--file", str(config_file)])
    assert result.exit_code == 0
    config_data = json.loads(result.output)
    assert config_data["backends"]["gemini"]["model"] == "gemini-pro-test"


def test_config_set_and_get(tmp_path: Path):
    """Test the 'config set' and 'config get' commands."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Set a value
    result = runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.model", "gemini-ultra-test"])
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
    runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.model", "gemini-ultra-test"])

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
    config.save_to_file(str(config_file))
    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration is valid" in result.output

    # Test with an invalid file
    with open(config_file, "w") as f:
        f.write("[backends.gemini]\nmodel = 123")  # model should be a string

    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration validation errors found" in result.output
    assert "gemini.model must be a string" in result.output


def test_config_migrate_no_env_vars(tmp_path: Path):
    """Test the 'config migrate' command with no environment variables."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a basic config file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Run migrate with no environment variables
    result = runner.invoke(main, ["config", "migrate", "--file", str(config_file)], input="n\n")

    assert result.exit_code == 0
    assert "No environment variables found to migrate" in result.output


def test_config_migrate_with_default_backend_env_var(tmp_path: Path):
    """Test migration of AUTO_CODER_DEFAULT_BACKEND environment variable."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with default backend environment variable
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_DEFAULT_BACKEND=gemini" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the value was saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "gemini"


def test_config_migrate_with_backend_api_key_env_vars(tmp_path: Path):
    """Test migration of backend-specific API key environment variables."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with API key environment variables
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={
            "AUTO_CODER_GEMINI_API_KEY": "test-gemini-key",
            "AUTO_CODER_QWEN_API_KEY": "test-qwen-key",
        },
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_GEMINI_API_KEY=***" in result.output
    assert "AUTO_CODER_QWEN_API_KEY=***" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the values were saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    gemini_config = config.get_backend_config("gemini")
    qwen_config = config.get_backend_config("qwen")
    assert gemini_config is not None
    assert qwen_config is not None
    assert gemini_config.api_key == "test-gemini-key"
    assert qwen_config.api_key == "test-qwen-key"


def test_config_migrate_with_openai_api_key_env_var(tmp_path: Path):
    """Test migration of AUTO_CODER_OPENAI_API_KEY environment variable."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with OpenAI API key
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_OPENAI_API_KEY": "test-openai-key"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_OPENAI_API_KEY=***" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the value was saved (should apply to backends that support OpenAI)
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    codex_config = config.get_backend_config("codex")
    claude_config = config.get_backend_config("claude")
    qwen_config = config.get_backend_config("qwen")
    assert codex_config is not None
    assert claude_config is not None
    assert qwen_config is not None
    assert codex_config.openai_api_key == "test-openai-key"
    assert claude_config.openai_api_key == "test-openai-key"
    assert qwen_config.openai_api_key == "test-openai-key"


def test_config_migrate_with_openai_base_url_env_var(tmp_path: Path):
    """Test migration of AUTO_CODER_OPENAI_BASE_URL environment variable."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with OpenAI base URL
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_OPENAI_BASE_URL": "https://custom.openai.com"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_OPENAI_BASE_URL=https://custom.openai.com" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the value was saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    codex_config = config.get_backend_config("codex")
    claude_config = config.get_backend_config("claude")
    assert codex_config is not None
    assert claude_config is not None
    assert codex_config.openai_base_url == "https://custom.openai.com"
    assert claude_config.openai_base_url == "https://custom.openai.com"


def test_config_migrate_creates_backup(tmp_path: Path):
    """Test that config migrate creates a backup when file exists."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create an existing config file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-model"
    config.save_to_file(str(config_file))

    # Run migrate
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "qwen"},
    )

    assert result.exit_code == 0
    assert "Configuration saved successfully" in result.output

    # Verify a backup was created
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) >= 1

    # Verify original value in backup
    backup_content = backup_files[0].read_text()
    assert "original-model" in backup_content


def test_config_migrate_detects_multiple_env_vars(tmp_path: Path):
    """Test that migrate detects and lists multiple environment variables."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with multiple environment variables
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={
            "AUTO_CODER_DEFAULT_BACKEND": "claude",
            "AUTO_CODER_GEMINI_API_KEY": "key1",
            "AUTO_CODER_OPENAI_API_KEY": "key2",
        },
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_DEFAULT_BACKEND=claude" in result.output
    assert "AUTO_CODER_GEMINI_API_KEY=***" in result.output
    assert "AUTO_CODER_OPENAI_API_KEY=***" in result.output
    assert "Configuration saved successfully" in result.output


def test_config_migrate_user_declines_save(tmp_path: Path):
    """Test that migrate doesn't save when user declines."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create initial config
    config = LLMBackendConfiguration()
    config.default_backend = "codex"
    config.save_to_file(str(config_file))

    # Run migrate but decline to save
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="n\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_DEFAULT_BACKEND=gemini" in result.output
    # Should not show success message
    assert "Configuration saved successfully" not in result.output

    # Verify original value was not changed
    config_after = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_after.default_backend == "codex"


def test_config_migrate_creates_new_config(tmp_path: Path):
    """Test that migrate creates a new config file if none exists."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate without an existing config file
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "qwen"},
    )

    assert result.exit_code == 0
    assert "Creating new configuration" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the file was created
    assert config_file.exists()

    # Verify the value was saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "qwen"


def test_config_migrate_no_backup_when_new_file(tmp_path: Path):
    """Test that no backup is created when migrating to a new config file."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate without an existing config file
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
    )

    assert result.exit_code == 0

    # Verify no backup was created (since file didn't exist before)
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) == 0
