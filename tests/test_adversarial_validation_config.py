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
    def test_create_from_order(self, mock_build, mock_rank, mock_get_config):
        mock_config = MagicMock()
        mock_config.get_adversarial_validation_backend_order.return_value = ["claude", "antigravity"]
        mock_config.get_backend_adversarial_validation.return_value = None
        mock_config.get_model_for_backend.side_effect = lambda b: f"model-{b}"
        mock_get_config.return_value = mock_config
        mock_rank.return_value = ["claude", "antigravity"]

        mgr = create_adversarial_validation_backend_manager()
        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs["selected_backends"] == ["claude", "antigravity"]
        assert call_kwargs["primary_backend"] == "claude"

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.create_high_score_backend_manager")
    def test_fallback_to_high_score_when_not_configured(self, mock_create_high_score, mock_get_config):
        mock_config = MagicMock()
        mock_config.get_adversarial_validation_backend_order.return_value = []
        mock_config.get_backend_adversarial_validation.return_value = None
        mock_get_config.return_value = mock_config

        mock_high_score_mgr = MagicMock()
        mock_create_high_score.return_value = mock_high_score_mgr

        mgr = create_adversarial_validation_backend_manager()
        assert mgr == mock_high_score_mgr

    @patch("auto_coder.cli_helpers.get_llm_config")
    @patch("auto_coder.cli_helpers.create_high_score_backend_manager")
    @patch("auto_coder.cli_helpers.create_high_score_cloud_backend_manager")
    def test_returns_none_when_no_strong_backend_configured(self, mock_create_cloud, mock_create_high_score, mock_get_config):
        """Must return None rather than silently falling back to general default backend."""
        mock_config = MagicMock()
        mock_config.get_adversarial_validation_backend_order.return_value = []
        mock_config.get_backend_adversarial_validation.return_value = None
        mock_get_config.return_value = mock_config

        mock_create_high_score.return_value = None
        mock_create_cloud.return_value = None

        mgr = create_adversarial_validation_backend_manager()
        assert mgr is None
