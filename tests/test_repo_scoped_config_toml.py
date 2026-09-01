"""Unit tests for repository-scoped partial configuration overrides in config.toml."""

from pathlib import Path

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.llm_backend_config import (
    get_auto_update_enabled_from_config,
    get_github_action_log_max_length_from_config,
    get_isolate_single_test_on_failure_from_config,
    get_issue_allowlist_from_config,
    get_jules_issue_pr_timeout_hours_from_config,
    get_jules_pr_ci_timeout_hours_from_config,
    get_jules_wait_timeout_hours_from_config,
    get_pr_allowlist_from_config,
    get_pr_review_allowlist_from_config,
    load_app_config_data,
)


def test_auto_update_enabled_defaults_to_true(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[jules]\nenabled = false\n", encoding="utf-8")

    assert get_auto_update_enabled_from_config(config_path=str(config_path)) is True


@pytest.mark.parametrize("enabled", [True, False])
def test_auto_update_enabled_reads_config(tmp_path: Path, enabled: bool):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[auto_update]\nenabled = {str(enabled).lower()}\n",
        encoding="utf-8",
    )

    assert get_auto_update_enabled_from_config(config_path=str(config_path)) is enabled


class TestLoadAppConfigData:
    """Test loading and merging config.toml with repository overrides."""

    def test_load_base_config_only(self, tmp_path: Path):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[jules]\nwait_timeout_hours = 5\n\n[github]\nissue_allowlist = [123, 456]\n",
            encoding="utf-8",
        )
        data = load_app_config_data(config_path=str(config_path), repo_name=None)
        assert data.get("jules", {}).get("wait_timeout_hours") == 5
        assert data.get("github", {}).get("issue_allowlist") == [123, 456]

    def test_load_with_repo_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            "[jules]\nwait_timeout_hours = 2\nissue_pr_timeout_hours = 12\n\n[test]\nisolate_single_test_on_failure = false\n",
            encoding="utf-8",
        )

        repo_override_dir = auto_coder_dir / "owner" / "repo"
        repo_override_dir.mkdir(parents=True)
        repo_config = repo_override_dir / "config.toml"
        repo_config.write_text(
            "[jules]\nwait_timeout_hours = 8\n\n[test]\nisolate_single_test_on_failure = true\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HOME", str(home_dir))

        data = load_app_config_data(config_path=str(base_config), repo_name="owner/repo")
        assert data.get("jules", {}).get("wait_timeout_hours") == 8
        assert data.get("jules", {}).get("issue_pr_timeout_hours") == 12  # Inherited from base
        assert data.get("test", {}).get("isolate_single_test_on_failure") is True

    def test_repo_isolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text("[jules]\nwait_timeout_hours = 2\n", encoding="utf-8")

        repo_a_dir = auto_coder_dir / "owner" / "repo-a"
        repo_a_dir.mkdir(parents=True)
        (repo_a_dir / "config.toml").write_text("[jules]\nwait_timeout_hours = 10\n", encoding="utf-8")

        repo_b_dir = auto_coder_dir / "owner" / "repo-b"
        repo_b_dir.mkdir(parents=True)
        (repo_b_dir / "config.toml").write_text("[jules]\nwait_timeout_hours = 20\n", encoding="utf-8")

        monkeypatch.setenv("HOME", str(home_dir))

        data_a = load_app_config_data(config_path=str(base_config), repo_name="owner/repo-a")
        data_b = load_app_config_data(config_path=str(base_config), repo_name="owner/repo-b")
        data_none = load_app_config_data(config_path=str(base_config), repo_name=None)

        assert data_a.get("jules", {}).get("wait_timeout_hours") == 10
        assert data_b.get("jules", {}).get("wait_timeout_hours") == 20
        assert data_none.get("jules", {}).get("wait_timeout_hours") == 2


class TestAutomationConfigRepoScoping:
    """Test AutomationConfig getters and initialization with repository scoping."""

    def test_automation_config_getters_with_repo_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[jules]
wait_timeout_hours = 2
issue_pr_timeout_hours = 12
pr_ci_timeout_hours = 12

[github_action]
max_log_length = 50000

[test]
isolate_single_test_on_failure = false

[github]
issue_allowlist = [100]
pr_allowlist = [200]
pr_review_allowlist = [199175422]
""",
            encoding="utf-8",
        )

        repo_dir = auto_coder_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text(
            """
[jules]
wait_timeout_hours = 6

[github_action]
max_log_length = 100000

[test]
isolate_single_test_on_failure = true

[github]
issue_allowlist = [999]
pr_review_allowlist = [123456789]
""",
            encoding="utf-8",
        )

        monkeypatch.setenv("HOME", str(home_dir))

        assert get_jules_wait_timeout_hours_from_config(config_path=str(base_config), repo_name="owner/repo") == 6
        assert get_jules_issue_pr_timeout_hours_from_config(config_path=str(base_config), repo_name="owner/repo") == 12
        assert get_jules_pr_ci_timeout_hours_from_config(config_path=str(base_config), repo_name="owner/repo") == 12
        assert get_github_action_log_max_length_from_config(config_path=str(base_config), repo_name="owner/repo") == 100000
        assert get_isolate_single_test_on_failure_from_config(config_path=str(base_config), repo_name="owner/repo") is True
        assert get_issue_allowlist_from_config(config_path=str(base_config), repo_name="owner/repo") == [999]
        assert get_pr_allowlist_from_config(config_path=str(base_config), repo_name="owner/repo") == [200]
        assert get_pr_review_allowlist_from_config(config_path=str(base_config), repo_name="owner/repo") == [123456789]

    def test_automation_config_instance_with_repo_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[jules]
wait_timeout_hours = 2

[test]
isolate_single_test_on_failure = false
""",
            encoding="utf-8",
        )

        repo_dir = auto_coder_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text(
            """
[jules]
wait_timeout_hours = 9

[test]
isolate_single_test_on_failure = true
""",
            encoding="utf-8",
        )

        monkeypatch.setenv("HOME", str(home_dir))

        config = AutomationConfig(env_override=False, repo_name="owner/repo")
        assert config.repo_name == "owner/repo"
        assert config.JULES_WAIT_TIMEOUT_HOURS == 9
        assert config.ISOLATE_SINGLE_TEST_ON_FAILURE is True


class TestPRReviewAllowlistConfig:
    def test_absent_and_empty_authorize_nobody(self, tmp_path: Path):
        absent = tmp_path / "absent.toml"
        absent.write_text("[github]\n", encoding="utf-8")
        empty = tmp_path / "empty.toml"
        empty.write_text("[github]\npr_review_allowlist = []\n", encoding="utf-8")

        assert get_pr_review_allowlist_from_config(config_path=str(absent)) is None
        assert get_pr_review_allowlist_from_config(config_path=str(empty)) == []

    @pytest.mark.parametrize("invalid_value", ['["199175422"]', "[true]", "[0]", "[-1]", '["chatgpt-codex-connector[bot]"]'])
    def test_rejects_invalid_identity_ids(self, tmp_path: Path, invalid_value: str):
        config_path = tmp_path / "config.toml"
        config_path.write_text(f"[github]\npr_review_allowlist = {invalid_value}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="positive integer GitHub identity IDs"):
            get_pr_review_allowlist_from_config(config_path=str(config_path))
