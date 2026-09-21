"""Tests for adversarial validation configuration and backend manager initialization."""

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.cli_helpers import (
    _resolve_adversarial_validation_candidate_route,
    create_adversarial_validation_backend_manager,
    resolve_adversarial_validation_availability,
)
from auto_coder.codex_usage_checker import CodexWeeklyUsage
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

    def test_strong_and_ordinary_routes_are_distinct(self):
        config = LLMBackendConfiguration(
            backend_order=["high-score"],
            backend_adversarial_validation_order=["general-reviewer"],
            backend_pr_adversarial_validation_order=["ordinary-reviewer"],
            backend_strong_pr_adversarial_validation_order=["strong-reviewer"],
        )
        assert _resolve_adversarial_validation_candidate_route("strong_pr", config) == ["strong-reviewer"]
        assert _resolve_adversarial_validation_candidate_route("pr", config) == ["ordinary-reviewer"]

    def test_absent_strong_route_does_not_fall_back_to_ordinary(self):
        config = LLMBackendConfiguration(
            backend_order=["high-score"],
            backend_adversarial_validation_order=["general-reviewer"],
            backend_pr_adversarial_validation_order=["ordinary-reviewer"],
        )
        assert _resolve_adversarial_validation_candidate_route("strong_pr", config) == []


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


@pytest.mark.parametrize("validation_kind", ["issue", "pr", "strong_pr"])
def test_loaded_burst_strategy_is_used_by_every_availability_route(validation_kind):
    config = LLMBackendConfiguration.load_from_dict(
        {
            "quota_selection": {"strategy": "burst"},
            "backend_issue_adversarial_validation": {"order": ["codex-reviewer"]},
            "backend_pr_adversarial_validation": {"order": ["codex-reviewer"]},
            "backend_strong_pr_adversarial_validation": {"order": ["codex-reviewer"]},
            "backends": {
                "codex-reviewer": {
                    "backend_type": "codex",
                    "model": "gpt-5",
                    "enabled": True,
                }
            },
        }
    )
    usage = CodexWeeklyUsage(
        remaining_percent=12.0,
        reset_at=datetime.now(timezone.utc) + timedelta(days=2),
        days_until_reset=2,
        minimum_remaining_percent=15.0,
    )
    manager = MagicMock()

    with (
        patch("auto_coder.cli_helpers.get_llm_config", return_value=config),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
        patch("auto_coder.cli_helpers.build_backend_manager", return_value=manager),
    ):
        availability = resolve_adversarial_validation_availability(validation_kind)

    assert availability.backend_manager is manager
    assert availability.exhausted is False
    assert availability.retry_not_before_epoch is None
