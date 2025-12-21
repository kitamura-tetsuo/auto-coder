"""Tests for required options validation functionality."""

import pytest
from click.testing import CliRunner

from src.auto_coder.cli import main
from src.auto_coder.llm_backend_config import REQUIRED_OPTIONS_BY_BACKEND, BackendConfig, LLMBackendConfiguration


class TestRequiredOptionsValidation:
    """Test cases for required options validation."""

    def test_required_options_constant_is_defined(self):
        """Test that REQUIRED_OPTIONS_BY_BACKEND constant is defined."""
        assert REQUIRED_OPTIONS_BY_BACKEND is not None
        assert isinstance(REQUIRED_OPTIONS_BY_BACKEND, dict)
        assert "codex" in REQUIRED_OPTIONS_BY_BACKEND
        assert "claude" in REQUIRED_OPTIONS_BY_BACKEND
        assert "gemini" in REQUIRED_OPTIONS_BY_BACKEND
        assert "auggie" in REQUIRED_OPTIONS_BY_BACKEND
        assert "qwen" in REQUIRED_OPTIONS_BY_BACKEND
        assert "jules" in REQUIRED_OPTIONS_BY_BACKEND
        assert "codex-mcp" in REQUIRED_OPTIONS_BY_BACKEND

    def test_backend_config_validate_required_options_no_errors(self):
        """Test validation passes when all required options are present."""
        # Test codex backend with required option
        config = BackendConfig(name="codex")
        config.options = ["--dangerously-bypass-approvals-and-sandbox"]
        errors = config.validate_required_options()
        assert errors == []

    def test_backend_config_validate_required_options_missing_option(self):
        """Test validation fails when required option is missing."""
        # Test codex backend without required option
        config = BackendConfig(name="codex")
        config.options = []  # Missing required option
        errors = config.validate_required_options()
        assert len(errors) == 1
        assert "missing required option: --dangerously-bypass-approvals-and-sandbox" in errors[0]
        assert "Add to [backends.codex].options" in errors[0]

    def test_backend_config_validate_required_options_uses_backend_type(self):
        """Test validation uses backend_type when available."""
        # Custom backend with backend_type set to "claude"
        config = BackendConfig(name="my-claude-backend")
        config.backend_type = "claude"
        config.options = []  # Missing required options
        errors = config.validate_required_options()
        assert len(errors) == 2  # claude has 2 required options
        assert "missing required option: --dangerously-skip-permissions" in errors[0]
        assert "missing required option: --allow-dangerously-skip-permissions" in errors[1]

    def test_backend_config_validate_required_options_falls_back_to_name(self):
        """Test validation falls back to name when backend_type is None."""
        config = BackendConfig(name="gemini")
        config.backend_type = None
        config.options = []  # Missing required option
        errors = config.validate_required_options()
        assert len(errors) == 1
        assert "missing required option: --yolo" in errors[0]

    def test_backend_config_validate_multiple_required_options(self):
        """Test validation with multiple required options."""
        config = BackendConfig(name="claude")
        config.options = ["--dangerously-skip-permissions"]  # Missing one required option
        errors = config.validate_required_options()
        assert len(errors) == 1
        assert "missing required option: --allow-dangerously-skip-permissions" in errors[0]

    def test_backend_config_validate_all_required_options_present(self):
        """Test validation passes when all required options are present."""
        config = BackendConfig(name="claude")
        config.options = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
        errors = config.validate_required_options()
        assert errors == []

    def test_backend_config_validate_backend_with_no_required_options(self):
        """Test validation for backends with no required options (jules, codex-mcp)."""
        # Test jules (no required options)
        config = BackendConfig(name="jules")
        config.options = []
        errors = config.validate_required_options()
        assert errors == []

        # Test codex-mcp (no required options)
        config = BackendConfig(name="codex-mcp")
        config.options = []
        errors = config.validate_required_options()
        assert errors == []

    def test_backend_config_validate_unknown_backend(self):
        """Test validation for unknown backend (defaults to empty list)."""
        config = BackendConfig(name="unknown-backend")
        config.options = []
        errors = config.validate_required_options()
        assert errors == []  # Unknown backends have no required options

    def test_backend_config_validate_required_options_partial_match(self):
        """Test that partial option match doesn't pass validation."""
        config = BackendConfig(name="qwen")
        config.options = ["--yes"]  # Similar but not exact match
        errors = config.validate_required_options()
        assert len(errors) == 1
        assert "missing required option: -y" in errors[0]

    def test_config_validate_includes_required_options_check(self):
        """Test that config_validate function checks required options."""
        from src.auto_coder.cli_commands_config import config_validate

        config = LLMBackendConfiguration()
        # Set up a backend with missing required option
        config.get_backend_config("codex").options = []

        errors = config_validate(config)
        assert len(errors) > 0
        assert any("missing required option: --dangerously-bypass-approvals-and-sandbox" in err for err in errors)

    def test_config_validate_passes_with_all_required_options(self):
        """Test that config_validate passes when all required options are present."""
        from src.auto_coder.cli_commands_config import config_validate

        config = LLMBackendConfiguration()
        # Set up backends with all required options
        config.get_backend_config("codex").options = ["--dangerously-bypass-approvals-and-sandbox"]
        config.get_backend_config("claude").options = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
        config.get_backend_config("gemini").options = ["--yolo"]
        config.get_backend_config("qwen").options = ["-y"]
        config.get_backend_config("auggie").options = ["--print"]

        errors = config_validate(config)
        assert errors == []


class TestConfigValidateCommand:
    """Test cases for config validate CLI command."""

    def test_config_validate_detects_missing_required_options(self, tmp_path):
        """Test that config validate command detects missing required options."""
        import tomli_w

        config_file = tmp_path / "llm_config.toml"
        data = {
            "backend": {"default": "codex", "order": ["codex"]},
            "backends": {
                "codex": {
                    "enabled": True,
                    "model": "codex",
                    "options": [],  # Missing required option
                }
            },
        }
        with open(config_file, "wb") as fh:
            tomli_w.dump(data, fh)

        runner = CliRunner()
        result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
        assert result.exit_code == 0
        assert "Configuration validation errors found" in result.output
        assert "missing required option: --dangerously-bypass-approvals-and-sandbox" in result.output

    def test_config_validate_passes_with_required_options(self, tmp_path):
        """Test that config validate command passes with all required options."""
        import tomli_w

        config_file = tmp_path / "llm_config.toml"
        data = {
            "backend": {"default": "codex", "order": ["codex"]},
            "backends": {
                "codex": {
                    "enabled": True,
                    "model": "codex",
                    "options": ["--dangerously-bypass-approvals-and-sandbox"],
                },
                "gemini": {
                    "enabled": False,
                },
                "qwen": {
                    "enabled": False,
                },
                "auggie": {
                    "enabled": False,
                },
                "claude": {
                    "enabled": False,
                },
            },
        }
        with open(config_file, "wb") as fh:
            tomli_w.dump(data, fh)

        runner = CliRunner()
        result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
        assert result.exit_code == 0
        assert "Configuration is valid" in result.output

    def test_config_validate_multiple_backends_missing_options(self, tmp_path):
        """Test validation with multiple backends with missing options."""
        import tomli_w

        config_file = tmp_path / "llm_config.toml"
        data = {
            "backend": {"default": "gemini", "order": ["gemini", "qwen", "claude"]},
            "backends": {
                "gemini": {
                    "enabled": True,
                    "model": "gemini-2.5-pro",
                    "options": [],  # Missing --yolo
                },
                "qwen": {
                    "enabled": True,
                    "model": "qwen3-coder-plus",
                    "options": [],  # Missing -y
                },
                "claude": {
                    "enabled": True,
                    "model": "sonnet",
                    "options": [],  # Missing both required options
                },
            },
        }
        with open(config_file, "wb") as fh:
            tomli_w.dump(data, fh)

        runner = CliRunner()
        result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
        assert result.exit_code == 0
        assert "Configuration validation errors found" in result.output
        assert "missing required option: --yolo" in result.output
        assert "missing required option: -y" in result.output
        assert "missing required option: --dangerously-skip-permissions" in result.output

    def test_config_validate_backends_with_no_required_options(self, tmp_path):
        """Test validation with backends that have no required options."""
        import tomli_w

        config_file = tmp_path / "llm_config.toml"
        data = {
            "backend": {"default": "jules", "order": ["jules", "codex-mcp"]},
            "backends": {
                "jules": {
                    "enabled": True,
                    "model": "jules",
                    "options": [],
                },
                "codex-mcp": {
                    "enabled": True,
                    "model": "codex-mcp",
                    "options": [],
                },
                "codex": {
                    "enabled": False,
                },
                "gemini": {
                    "enabled": False,
                },
                "qwen": {
                    "enabled": False,
                },
                "auggie": {
                    "enabled": False,
                },
                "claude": {
                    "enabled": False,
                },
            },
        }
        with open(config_file, "wb") as fh:
            tomli_w.dump(data, fh)

        runner = CliRunner()
        result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
        assert result.exit_code == 0
        assert "Configuration is valid" in result.output

    def test_config_validate_custom_backend_with_backend_type(self, tmp_path):
        """Test validation with custom backend using backend_type."""
        import tomli_w

        config_file = tmp_path / "llm_config.toml"
        data = {
            "backend": {"default": "my-custom-codex", "order": ["my-custom-codex"]},
            "backends": {
                "my-custom-codex": {
                    "enabled": True,
                    "model": "custom-codex-model",
                    "backend_type": "codex",
                    "options": [],  # Missing required option for codex
                }
            },
        }
        with open(config_file, "wb") as fh:
            tomli_w.dump(data, fh)

        runner = CliRunner()
        result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
        assert result.exit_code == 0
        assert "Configuration validation errors found" in result.output
        assert "missing required option: --dangerously-bypass-approvals-and-sandbox" in result.output
        assert "Add to [backends.my-custom-codex].options" in result.output
