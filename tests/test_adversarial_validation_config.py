"""Tests for adversarial validation configuration and backend manager initialization."""

import os
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.cli_helpers import create_adversarial_validation_backend_manager
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestAdversarialValidationConfiguration:
    """Test loading, saving, and getters for [backend_adversarial_validation]."""

    def test_load_from_dict_order_and_default(self):
        data = {
            "backend": {"order": ["codex"], "default": "codex"},
            "backend_adversarial_validation": {
                "order": ["claude", "antigravity"],
                "default": "claude",
            },
            "backends": {
                "claude": {"enabled": True, "model": "claude-3-opus"},
                "antigravity": {"enabled": True, "model": "gemini-2.5-pro"},
                "codex": {"enabled": True, "model": "codex"},
            },
        }
        config = LLMBackendConfiguration.load_from_dict(data)

        assert config.get_adversarial_validation_backend_order() == ["claude", "antigravity"]
        assert config.get_adversarial_validation_default_backend() == "claude"

    def test_load_from_dict_single_backend_config(self):
        data = {
            "backend": {"order": ["codex"], "default": "codex"},
            "backend_adversarial_validation": {
                "name": "custom-validator",
                "model": "gpt-5-strong",
                "enabled": True,
            },
            "backends": {
                "codex": {"enabled": True, "model": "codex"},
            },
        }
        config = LLMBackendConfiguration.load_from_dict(data)

        adv_backend = config.get_backend_adversarial_validation()
        assert adv_backend is not None
        assert adv_backend.name == "custom-validator"
        assert adv_backend.model == "gpt-5-strong"
        assert config.get_model_for_backend_adversarial_validation() == "gpt-5-strong"

    def test_save_and_reload_configuration(self, tmp_path):
        config_path = str(tmp_path / "llm_config.toml")
        config = LLMBackendConfiguration(
            backend_order=["codex"],
            default_backend="codex",
            backend_adversarial_validation_order=["claude", "antigravity"],
            backend_adversarial_validation_default="claude",
            config_file_path=config_path,
        )
        config.save_to_file(config_path)

        reloaded = LLMBackendConfiguration.load_from_file(config_path)
        assert reloaded.get_adversarial_validation_backend_order() == ["claude", "antigravity"]
        assert reloaded.get_adversarial_validation_default_backend() == "claude"

    def test_automation_config_env_override(self, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", "false")
        config = AutomationConfig()
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

        monkeypatch.setenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", "true")
        config2 = AutomationConfig()
        assert config2.ENABLE_ADVERSARIAL_VALIDATION is True


class TestCreateAdversarialValidationBackendManager:
    """Test factory for creating adversarial validation backend manager."""

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.quota_selector.rank_high_score_backends_by_quota")
    @patch("auto_coder.cli_helpers.build_backend_manager")
    def test_create_from_order_filters_read_only_capable(self, mock_build, mock_rank, mock_get_config):
        """Order must filter out cloud/non-enforcing backends and only include read-only capable ones."""
        mock_config = MagicMock()
        # Order includes cloud agents and capable local backends
        mock_config.get_adversarial_validation_backend_order.return_value = ["codex_cloud", "claude", "claude_routine", "codex"]
        mock_config.get_backend_adversarial_validation.return_value = None
        mock_config.get_model_for_backend.side_effect = lambda b: f"model-{b}"
        mock_get_config.return_value = mock_config
        mock_rank.return_value = ["claude", "codex"]

        mgr = create_adversarial_validation_backend_manager()
        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs["selected_backends"] == ["claude", "codex"]
        assert call_kwargs["primary_backend"] == "claude"

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.create_high_score_backend_manager", return_value=None)
    @patch("auto_coder.cli_helpers.build_backend_manager")
    def test_rejects_cloud_and_non_enforcing_single_backend_config(self, mock_build, mock_high_score, mock_get_config):
        """Single backend config with cloud backend must be rejected (returns None)."""
        mock_config = MagicMock()
        mock_config.get_adversarial_validation_backend_order.return_value = []
        adv_backend = MagicMock()
        adv_backend.name = "codex_cloud"
        adv_backend.model = "cloud-model"
        mock_config.get_backend_adversarial_validation.return_value = adv_backend
        mock_get_config.return_value = mock_config

        mgr = create_adversarial_validation_backend_manager()
        assert mgr is None
        mock_build.assert_not_called()

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.create_high_score_backend_manager")
    def test_fallback_to_high_score_when_not_configured(self, mock_create_high_score, mock_get_config):
        mock_config = MagicMock()
        mock_config.get_adversarial_validation_backend_order.return_value = []
        mock_config.get_backend_adversarial_validation.return_value = None
        mock_get_config.return_value = mock_config

        mock_high_score_mgr = MagicMock()
        mock_high_score_mgr._current_backend_name.return_value = "claude"
        mock_create_high_score.return_value = mock_high_score_mgr

        mgr = create_adversarial_validation_backend_manager()
        assert mgr == mock_high_score_mgr

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.create_high_score_backend_manager")
    def test_returns_none_when_no_strong_backend_configured(self, mock_create_high_score, mock_get_config):
        """Must return None rather than falling back to cloud backends or general default backend."""
        mock_config = MagicMock()
        mock_config.get_adversarial_validation_backend_order.return_value = []
        mock_config.get_backend_adversarial_validation.return_value = None
        mock_get_config.return_value = mock_config

        mock_create_high_score.return_value = None

        mgr = create_adversarial_validation_backend_manager()
        assert mgr is None

    def test_is_read_only_review_capable_backend(self):
        """Verify capability filtering for synchronous read-only review."""
        from auto_coder.cli_helpers import is_read_only_review_capable_backend

        # Capable local backends
        assert is_read_only_review_capable_backend("claude") is True
        assert is_read_only_review_capable_backend("claude-3-5-sonnet") is True
        assert is_read_only_review_capable_backend("codex") is True
        assert is_read_only_review_capable_backend("codex_o3") is True

        # Ineligible MCP variants, cloud backends, routines, and non-enforcing clients
        assert is_read_only_review_capable_backend("codex_mcp") is False
        assert is_read_only_review_capable_backend("codex-mcp") is False
        assert is_read_only_review_capable_backend("codex_cloud") is False
        assert is_read_only_review_capable_backend("codex-cloud") is False
        assert is_read_only_review_capable_backend("claude_routine") is False
        assert is_read_only_review_capable_backend("claude-routine") is False
        assert is_read_only_review_capable_backend("jules") is False
        assert is_read_only_review_capable_backend("aider") is False
        assert is_read_only_review_capable_backend("auggie") is False
        assert is_read_only_review_capable_backend(None) is False
        assert is_read_only_review_capable_backend("") is False

    def test_is_read_only_review_capable_backend_with_backend_type_resolution(self):
        """Verify that capability is determined by resolved backend_type rather than alias string."""
        from auto_coder.cli_helpers import is_read_only_review_capable_backend

        mock_config = MagicMock()

        # Alias looks like codex, but backend_type is codex-cloud -> MUST BE REJECTED
        mock_b1 = MagicMock(backend_type="codex-cloud")
        # Alias looks unfamiliar, but backend_type is claude -> MUST BE ACCEPTED
        mock_b2 = MagicMock(backend_type="claude")

        mock_config.get_backend_config.side_effect = lambda name: {
            "codex-heavy": mock_b1,
            "custom-reviewer": mock_b2,
        }.get(name)

        assert is_read_only_review_capable_backend("codex-heavy", mock_config) is False
        assert is_read_only_review_capable_backend("custom-reviewer", mock_config) is True
