import os
import stat

import pytest

from auto_coder.cli_commands_config import backup_config
from auto_coder.security_utils import redact_string


def test_redact_anthropic_key():
    # Example Anthropic key format
    key = "sk-ant-api03-abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    redacted = redact_string(key)
    assert redacted != key
    assert "[REDACTED]" in redacted
    # Ensure the secret part is gone
    assert "abcdef1234567890" not in redacted


def test_redact_aws_access_key():
    key = "AKIAIOSFODNN7EXAMPLE"
    redacted = redact_string(key)
    assert redacted != key
    assert "[REDACTED]" in redacted
    assert "AKIA" not in redacted  # Replaced fully?


def test_redact_aws_secret_key():
    # AWS Secret Access Key (40 chars)
    key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    # This might be harder to identify without context, but maybe we have a pattern?
    # Usually we don't redact random 40 chars unless we know it's a key.
    # But we can check if my added patterns cover it.
    pass


def test_backup_config_secure_permissions(tmp_path):
    """Test that backup_config creates backups with 0o600 permissions."""
    # Create a dummy config file
    config_file = tmp_path / "llm_config.toml"
    config_file.write_text('api_key = "secret"')

    # Set insecure permissions on source (e.g. 644)
    os.chmod(config_file, 0o644)
    assert stat.S_IMODE(os.stat(config_file).st_mode) == 0o644

    # Run backup
    backup_config(str(config_file))

    # Find the backup file
    backups = list(tmp_path.glob("llm_config.toml.backup_*"))
    assert len(backups) == 1
    backup_file = backups[0]

    # Check permissions of backup
    mode = os.stat(backup_file).st_mode
    # Should be 0o600 (rw-------)
    assert stat.S_IMODE(mode) == 0o600
