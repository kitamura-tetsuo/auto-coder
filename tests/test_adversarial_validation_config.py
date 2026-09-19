"""Tests for adversarial validation configuration and backend manager initialization."""

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.cli_helpers import create_adversarial_validation_backend_manager, resolve_adversarial_validation_availability
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.quota_selector import BackendQuotaEvaluation


class TestStrongPRAdversarialValidationConfiguration:
    """Test loading, saving, and getters for [backend_strong_pr_adversarial_validation]."""

    def test_load_from_dict_order_and_default(self):
        data = {
            "backend": {"order": ["codex"], "default": "codex"},
            "backend_pr_adversarial_validation": {
                "order": ["claude", "antigravity"],
                "default": "claude",
            },
            "backend_strong_pr_adversarial_validation": {
                "order": ["muse", "codex"],
                "default": "muse",
            },
            "backends": {
                "claude": {"enabled": True, "model": "claude-3-opus"},
                "antigravity": {"enabled": True, "model": "gemini-2.5-pro"},
                "codex": {"enabled": True, "model": "codex"},
                "muse": {"enabled": True, "model": "muse-spark-1.3"},
            },
        }
        config = LLMBackendConfiguration.load_from_dict(data)

        assert config.get_strong_pr_adversarial_validation_backend_order() == ["muse", "codex"]
        assert config.get_strong_pr_adversarial_validation_default_backend() == "muse"

    def test_load_from_dict_single_backend_config(self):
        data = {
            "backend": {"order": ["codex"], "default": "codex"},
            "backend_pr_adversarial_validation": {
                "order": ["claude", "antigravity"],
                "default": "claude",
            },
            "backend_strong_pr_adversarial_validation": {
                "name": "strong-validator",
                "model": "gpt-5-strong",
                "enabled": True,
            },
            "backends": {
                "codex": {"enabled": True, "model": "codex"},
            },
        }
        config = LLMBackendConfiguration.load_from_dict(data)

        adv_backend = config.get_backend_strong_pr_adversarial_validation()
        assert adv_backend is not None
        assert adv_backend.name == "strong-validator"
        assert adv_backend.model == "gpt-5-strong"
        assert config.get_model_for_backend_strong_pr_adversarial_validation() == "gpt-5-strong"

    def test_save_and_reload_configuration(self, tmp_path):
        config_path = str(tmp_path / "llm_config.toml")
        config = LLMBackendConfiguration(
            backend_order=["codex"],
            default_backend="codex",
            backend_pr_adversarial_validation_order=["claude", "antigravity"],
            backend_pr_adversarial_validation_default="claude",
            backend_strong_pr_adversarial_validation_order=["muse", "codex"],
            backend_strong_pr_adversarial_validation_default="muse",
            config_file_path=config_path,
        )
        config.save_to_file(config_path)

        reloaded = LLMBackendConfiguration.load_from_file(config_path)
        assert reloaded.get_strong_pr_adversarial_validation_backend_order() == ["muse", "codex"]
        assert reloaded.get_strong_pr_adversarial_validation_default_backend() == "muse"


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

    def test_automation_config_max_adversarial_validations_env_override(self, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_MAX_ADVERSARIAL_VALIDATIONS", "5")
        config = AutomationConfig()
        assert config.MAX_ADVERSARIAL_VALIDATIONS == 5
        assert config.MAX_ADVERSARIAL_REVIEWS == 5

        monkeypatch.setenv("AUTO_CODER_MAX_ADVERSARIAL_REVIEWS", "2")
        monkeypatch.delenv("AUTO_CODER_MAX_ADVERSARIAL_VALIDATIONS", raising=False)
        config2 = AutomationConfig()
        assert config2.MAX_ADVERSARIAL_VALIDATIONS == 2
        assert config2.MAX_ADVERSARIAL_REVIEWS == 2

    def test_max_adversarial_validations_from_config_toml(self, tmp_path):
        from auto_coder.llm_backend_config import get_adversarial_validation_max_reviews_from_config

        config_path = str(tmp_path / "config.toml")
        with open(config_path, "w") as f:
            f.write("[adversarial_validation]\nmax_reviews = 3\n")

        assert get_adversarial_validation_max_reviews_from_config(config_path=config_path) == 3

    def test_max_adversarial_validations_fallback_keys(self, tmp_path):
        from auto_coder.llm_backend_config import get_adversarial_validation_max_reviews_from_config

        config_path = str(tmp_path / "config.toml")
        with open(config_path, "w") as f:
            f.write("[adversarial_validation]\nmax_validations = 4\n")
        assert get_adversarial_validation_max_reviews_from_config(config_path=config_path) == 4

        with open(config_path, "w") as f:
            f.write("[backend_adversarial_validation]\nmax_reviews = 2\n")
        assert get_adversarial_validation_max_reviews_from_config(config_path=config_path) == 2

    def test_count_adversarial_validation_comments(self):
        from auto_coder.adversarial_validator import count_adversarial_validation_comments

        comments = [
            {"body": "Regular review comment"},
            {"body": "<!-- auto-coder-adversarial-validation:v4:abc1234 -->\n## ✅ Auto-Coder adversarial validation: PASS"},
            {"body": "<!-- auto-coder-adversarial-validation-codex-feedback:v4:abc1234 -->\nFeedback sent to cloud"},
            {"body": "<!-- auto-coder-adversarial-validation:v4:def5678 -->\n## ❌ Auto-Coder adversarial validation: NEEDS_FIX"},
        ]
        assert count_adversarial_validation_comments(comments) == 2
        assert count_adversarial_validation_comments([]) == 0
        assert count_adversarial_validation_comments(None) == 0
