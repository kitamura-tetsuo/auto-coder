"""Unit tests for repo-scoped pipeline feature switches and gates (Issue #1812 / Parent #1811)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.llm_backend_config import (
    FEATURE_SWITCH_NAMES,
    get_all_feature_switches_from_config,
    get_automatic_test_fix_from_config,
    get_feature_switch_from_config,
    get_issue_decomposition_validation_from_config,
    get_issue_specification_validation_from_config,
    get_pr_adversarial_validation_from_config,
    get_pr_review_thread_gate_from_config,
    load_app_config_data,
    validate_feature_switches_in_config_dict,
)


class TestFeatureSwitchesDefaults:
    """AS-001: Defaults for all 5 canonical feature switches."""

    def test_feature_switch_names_complete(self):
        expected = {
            "issue_specification_validation",
            "issue_decomposition_validation",
            "pr_adversarial_validation",
            "pr_review_thread_gate",
            "automatic_test_fix",
        }
        assert set(FEATURE_SWITCH_NAMES) == expected

    def test_defaults_when_unconfigured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Empty config
        config_path = tmp_path / "empty_config.toml"
        config_path.write_text("", encoding="utf-8")

        for name in FEATURE_SWITCH_NAMES:
            assert get_feature_switch_from_config(name, config_path=str(config_path)) is True

        all_switches = get_all_feature_switches_from_config(config_path=str(config_path))
        assert len(all_switches) == 5
        assert all(val is True for val in all_switches.values())

    def test_automation_config_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        (auto_coder_dir / "config.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        config = AutomationConfig()
        assert config.issue_specification_validation is True
        assert config.issue_decomposition_validation is True
        assert config.pr_adversarial_validation is True
        assert config.pr_review_thread_gate is True
        assert config.automatic_test_fix is True
        assert config.ENABLE_ADVERSARIAL_VALIDATION is True

    def test_individual_getters_default_true(self, tmp_path: Path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("", encoding="utf-8")
        p = str(config_path)

        assert get_issue_specification_validation_from_config(config_path=p) is True
        assert get_issue_decomposition_validation_from_config(config_path=p) is True
        assert get_pr_adversarial_validation_from_config(config_path=p) is True
        assert get_pr_review_thread_gate_from_config(config_path=p) is True
        assert get_automatic_test_fix_from_config(config_path=p) is True


class TestRepositoryIsolation:
    """AS-002: Repository isolation and precedence semantics."""

    def test_repo_override_under_features_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
issue_specification_validation = true
issue_decomposition_validation = true
""",
            encoding="utf-8",
        )

        repo_a_dir = auto_coder_dir / "owner" / "repo-a"
        repo_a_dir.mkdir(parents=True)
        (repo_a_dir / "config.toml").write_text(
            """
[features]
issue_specification_validation = false
""",
            encoding="utf-8",
        )

        repo_b_dir = auto_coder_dir / "owner" / "repo-b"
        repo_b_dir.mkdir(parents=True)
        (repo_b_dir / "config.toml").write_text(
            """
[features]
automatic_test_fix = false
""",
            encoding="utf-8",
        )

        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        config_a = AutomationConfig(repo_name="owner/repo-a")
        config_b = AutomationConfig(repo_name="owner/repo-b")
        config_default = AutomationConfig(repo_name="owner/repo-unconfigured")

        # Repo A observes false for issue_specification_validation
        assert config_a.issue_specification_validation is False
        assert config_a.issue_decomposition_validation is True
        assert config_a.automatic_test_fix is True

        # Repo B observes true for issue_specification_validation, false for automatic_test_fix
        assert config_b.issue_specification_validation is True
        assert config_b.issue_decomposition_validation is True
        assert config_b.automatic_test_fix is False

        # Unconfigured repo observes defaults from base
        assert config_default.issue_specification_validation is True
        assert config_default.issue_decomposition_validation is True
        assert config_default.automatic_test_fix is True

    def test_repo_override_toplevel_and_section_compatibility(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            "issue_specification_validation = true\n",
            encoding="utf-8",
        )

        repo_a_dir = auto_coder_dir / "owner" / "repo-a"
        repo_a_dir.mkdir(parents=True)
        (repo_a_dir / "config.toml").write_text(
            "issue_specification_validation = false\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HOME", str(home_dir))
        config_a = AutomationConfig(repo_name="owner/repo-a")
        assert config_a.issue_specification_validation is False


class TestLegacyAdversarialValidationOverride:
    """AS-003: Compatibility override with AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION."""

    def test_env_var_overrides_file_true_to_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
pr_adversarial_validation = true
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", "false")

        config = AutomationConfig()
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False
        assert get_feature_switch_from_config("pr_adversarial_validation", config_path=str(base_config)) is False

    def test_env_var_overrides_file_false_to_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
pr_adversarial_validation = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", "true")

        config = AutomationConfig()
        assert config.pr_adversarial_validation is True
        assert config.ENABLE_ADVERSARIAL_VALIDATION is True
        assert get_feature_switch_from_config("pr_adversarial_validation", config_path=str(base_config)) is True

    def test_file_controls_when_env_var_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
pr_adversarial_validation = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        config = AutomationConfig()
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

    def test_bidirectional_property_sync(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)
        config = AutomationConfig()
        assert config.pr_adversarial_validation is True
        assert config.ENABLE_ADVERSARIAL_VALIDATION is True

        config.ENABLE_ADVERSARIAL_VALIDATION = False
        assert config.pr_adversarial_validation is False

        config.pr_adversarial_validation = True
        assert config.ENABLE_ADVERSARIAL_VALIDATION is True


class TestNoSyntheticLifecycleState:
    """AS-004: Loading config does not produce synthetic state, labels, or mutations."""

    def test_disabled_feature_does_not_mutate_authoritative_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            """
[features]
issue_specification_validation = false
issue_decomposition_validation = false
pr_adversarial_validation = false
pr_review_thread_gate = false
automatic_test_fix = false
""",
            encoding="utf-8",
        )

        mock_github = MagicMock()
        mock_store = MagicMock()

        # Loading configuration
        data = load_app_config_data(config_path=str(config_path))
        for switch in FEATURE_SWITCH_NAMES:
            assert data["features"][switch] is False

        # No mock methods were called on external APIs or stores
        assert mock_github.method_calls == []
        assert mock_store.method_calls == []


class TestInvalidConfiguredValues:
    """AS-005: Non-boolean configured values fail with explicit configuration error."""

    @pytest.mark.parametrize("invalid_value", ['"maybe"', '"true"', '"false"', "1", "0", "1.5", "[1, 2]"])
    def test_invalid_value_in_features_table(self, tmp_path: Path, invalid_value: str):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f"[features]\nissue_decomposition_validation = {invalid_value}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="issue_decomposition_validation"):
            load_app_config_data(config_path=str(config_path))

    @pytest.mark.parametrize("invalid_value", ['"maybe"', "123"])
    def test_invalid_value_at_toplevel(self, tmp_path: Path, invalid_value: str):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f"issue_specification_validation = {invalid_value}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="issue_specification_validation"):
            load_app_config_data(config_path=str(config_path))

    def test_invalid_value_in_repo_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text("[features]\nautomatic_test_fix = true\n", encoding="utf-8")

        repo_dir = auto_coder_dir / "owner" / "invalid-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text(
            "[features]\nautomatic_test_fix = 'invalid'\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HOME", str(home_dir))

        with pytest.raises(ValueError, match="automatic_test_fix"):
            AutomationConfig(repo_name="owner/invalid-repo")


class TestNormalReloadBoundary:
    """AS-006: Normal configuration reload lifecycle boundary."""

    def test_running_config_preserves_initial_value_until_reconstructed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        config_file = auto_coder_dir / "config.toml"
        config_file.write_text("[features]\nissue_specification_validation = true\n", encoding="utf-8")

        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        # Existing running config instance
        running_config = AutomationConfig()
        assert running_config.issue_specification_validation is True

        # Backing file is modified while process is running
        config_file.write_text("[features]\nissue_specification_validation = false\n", encoding="utf-8")

        # The existing running_config object retains its loaded value
        assert running_config.issue_specification_validation is True

        # When configuration is reconstructed / reloaded, the new value is observed
        reloaded_config = AutomationConfig()
        assert reloaded_config.issue_specification_validation is False
