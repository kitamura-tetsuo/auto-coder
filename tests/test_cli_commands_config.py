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

    # Test with a valid file (add required options for enabled backends)
    config = LLMBackendConfiguration()
    # Add required options for all enabled backends
    config.get_backend_config("codex").options = ["--dangerously-bypass-approvals-and-sandbox"]
    config.get_backend_config("claude").options = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
    config.get_backend_config("gemini").options = ["--yolo"]
    config.get_backend_config("qwen").options = ["-y"]
    config.get_backend_config("auggie").options = ["--print"]
    config.save_to_file(config_file)
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


def test_config_export_permissions(tmp_path: Path):
    """Test the 'config export' command for secure file permissions."""
    config_file = tmp_path / "llm_backend.toml"
    output_file = tmp_path / "exported_config.json"
    runner = CliRunner()

    # Create a dummy config file
    config = LLMBackendConfiguration()
    config.save_to_file(config_file)

    # Test export with secure permissions
    result = runner.invoke(
        main,
        ["config", "export", "--file", str(config_file), "--output", str(output_file)],
    )
    assert result.exit_code == 0
    assert f"Configuration exported to: {output_file}" in result.output

    # Verify file exists
    assert output_file.exists()

    # Check file permissions (POSIX only)
    if os.name == "posix":
        import stat

        st = os.stat(output_file)
        permissions = stat.S_IMODE(st.st_mode)
        # Should be readable/writable only by owner (0o600)
        assert permissions & 0o077 == 0, f"File permissions {oct(permissions)} are insecure"
        assert permissions & 0o600 == 0o600, "File should be readable/writable by owner"


def test_config_export_overwrite_permissions(tmp_path: Path):
    """Test that 'config export' fixes insecure permissions on overwrite."""
    if os.name != "posix":
        return

    import stat

    config_file = tmp_path / "llm_backend.toml"
    output_file = tmp_path / "exported_config.json"
    runner = CliRunner()

    # Create a dummy config file
    config = LLMBackendConfiguration()
    config.save_to_file(config_file)

    # Pre-create output file with insecure permissions (world readable)
    output_file.touch()
    os.chmod(output_file, 0o666)

    # Verify it is insecure
    st = os.stat(output_file)
    assert stat.S_IMODE(st.st_mode) & 0o066 != 0

    # Test export overwriting the file
    result = runner.invoke(
        main,
        ["config", "export", "--file", str(config_file), "--output", str(output_file)],
    )
    assert result.exit_code == 0

    # Check file permissions are fixed
    st = os.stat(output_file)
    permissions = stat.S_IMODE(st.st_mode)
    assert permissions & 0o077 == 0, f"File permissions {oct(permissions)} are insecure after overwrite"
    assert permissions & 0o600 == 0o600
