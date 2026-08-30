"""Unit tests for repository-scoped partial configuration overrides in llm_config.toml."""

import os
import tempfile

import pytest

from auto_coder.claude_routine_client import ClaudeRoutineClient
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.llm_backend_config import (
    BackendConfig,
    LLMBackendConfiguration,
    active_repo_context,
    deep_merge_config_dict,
    get_active_repo_name,
    get_llm_config,
    reset_llm_config,
    resolve_repo_override_path,
    set_active_repo_name,
)


class TestResolveRepoOverridePath:
    """Test resolution of repository override TOML paths."""

    def test_valid_owner_repo(self):
        base_path = "/tmp/test_dir/llm_config.toml"
        result = resolve_repo_override_path("kitamura-tetsuo/auto-coder", base_config_path=base_path)
        assert result == "/tmp/test_dir/kitamura-tetsuo/auto-coder/llm_config.toml"

    def test_valid_owner_repo_with_git_suffix(self):
        base_path = "/tmp/test_dir/llm_config.toml"
        result = resolve_repo_override_path("kitamura-tetsuo/auto-coder.git", base_config_path=base_path)
        assert result == "/tmp/test_dir/kitamura-tetsuo/auto-coder/llm_config.toml"

    def test_default_base_path(self):
        result = resolve_repo_override_path("owner/repo")
        expected_dir = os.path.expanduser("~/.auto-coder")
        assert result == os.path.join(expected_dir, "owner", "repo", "llm_config.toml")

    def test_invalid_short_repo_name(self):
        assert resolve_repo_override_path("auto-coder") is None
        assert resolve_repo_override_path("") is None
        assert resolve_repo_override_path(None) is None  # type: ignore[arg-type]

    def test_invalid_malformed_repo_names(self):
        assert resolve_repo_override_path("/") is None
        assert resolve_repo_override_path("/repo") is None
        assert resolve_repo_override_path("owner/") is None


class TestDeepMergeConfigDict:
    """Test recursive merging behavior for TOML dictionaries."""

    def test_scalar_replacement(self):
        base = {"model": "gpt-4", "attempts": 1, "enabled": True}
        override = {"model": "gpt-5", "attempts": 3}
        merged = deep_merge_config_dict(base, override)
        assert merged == {"model": "gpt-5", "attempts": 3, "enabled": True}
        # Verify base dictionary is unmutated
        assert base["model"] == "gpt-4"

    def test_nested_table_recursive_merge(self):
        base = {
            "backends": {
                "codex": {"model": "codex-v1", "temperature": 0.2, "timeout": 30},
                "gemini": {"model": "gemini-2.5", "temperature": 0.5},
            }
        }
        override = {
            "backends": {
                "codex": {"model": "codex-v2", "temperature": 0.0},
            }
        }
        merged = deep_merge_config_dict(base, override)
        assert merged["backends"]["codex"]["model"] == "codex-v2"
        assert merged["backends"]["codex"]["temperature"] == 0.0
        assert merged["backends"]["codex"]["timeout"] == 30
        assert merged["backends"]["gemini"]["model"] == "gemini-2.5"

    def test_list_replaced_as_whole(self):
        base = {"backend": {"order": ["codex", "gemini", "claude"]}}
        override = {"backend": {"order": ["claude", "qwen"]}}
        merged = deep_merge_config_dict(base, override)
        assert merged["backend"]["order"] == ["claude", "qwen"]

    def test_empty_override(self):
        base = {"backend": {"default": "codex"}, "backends": {"codex": {"model": "default-model"}}}
        merged = deep_merge_config_dict(base, {})
        assert merged == base

    def test_incompatible_type_dict_vs_scalar_raises_value_error(self):
        base = {"backends": {"codex": {"environment": "env-1"}}}
        override = {"backends": {"codex": "not-a-dict"}}
        with pytest.raises(ValueError, match="Incompatible override type"):
            deep_merge_config_dict(base, override)

    def test_incompatible_type_scalar_vs_dict_raises_value_error(self):
        base = {"backends": {"codex": "not-a-dict"}}
        override = {"backends": {"codex": {"environment": "env-1"}}}
        with pytest.raises(ValueError, match="Incompatible override type"):
            deep_merge_config_dict(base, override)

    def test_incompatible_type_list_vs_scalar_raises_value_error(self):
        base = {"order": ["codex"]}
        override = {"order": "codex"}
        with pytest.raises(ValueError, match="Incompatible override type"):
            deep_merge_config_dict(base, override)


class TestRepoScopedLLMBackendConfiguration:
    """Test LLMBackendConfiguration loading with repository-specific overrides."""

    @pytest.fixture(autouse=True)
    def clean_env_and_config(self):
        reset_llm_config()
        set_active_repo_name(None)
        yield
        reset_llm_config()
        set_active_repo_name(None)

    def test_load_from_file_with_repo_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_config_path = os.path.join(tmpdir, "llm_config.toml")
            repo_override_dir = os.path.join(tmpdir, "kitamura-tetsuo", "auto-coder")
            os.makedirs(repo_override_dir, exist_ok=True)
            repo_override_path = os.path.join(repo_override_dir, "llm_config.toml")

            # Write base config
            with open(base_config_path, "w", encoding="utf-8") as f:
                f.write(
                    """
[backend]
default = "codex"
order = ["codex", "claude"]

[backends.codex_cloud]
model = "codex-base"
environment_id = "env-outliner-123"
attempts = 1

[backends.claude_routine]
url = "https://base.routine.anthropic.com"
"""
                )

            # Write repo override for kitamura-tetsuo/auto-coder
            with open(repo_override_path, "w", encoding="utf-8") as f:
                f.write(
                    """
[backends.codex_cloud]
environment = "env-autocoder-999"
attempts = 3

[backends.claude_routine]
url = "https://custom.routine.anthropic.com"
"""
                )

            # 1. Loading without repo returns base config
            base_loaded = LLMBackendConfiguration.load_from_file(config_path=base_config_path)
            codex_base = base_loaded.get_backend_config("codex_cloud")
            assert codex_base is not None
            assert codex_base.environment_id == "env-outliner-123"
            assert codex_base.environment == "env-outliner-123"
            assert codex_base.attempts == 1
            assert codex_base.model == "codex-base"

            # 2. Loading with repo returns merged config
            repo_loaded = LLMBackendConfiguration.load_from_file(
                config_path=base_config_path,
                repo_name="kitamura-tetsuo/auto-coder",
            )
            codex_repo = repo_loaded.get_backend_config("codex_cloud")
            assert codex_repo is not None
            assert codex_repo.environment_id == "env-autocoder-999"
            assert codex_repo.environment == "env-autocoder-999"
            assert codex_repo.attempts == 3
            assert codex_repo.model == "codex-base"  # Preserved from base!

            claude_repo = repo_loaded.get_backend_config("claude_routine")
            assert claude_repo is not None
            assert claude_repo.url == "https://custom.routine.anthropic.com"

    def test_load_from_file_invalid_toml_override_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_config_path = os.path.join(tmpdir, "llm_config.toml")
            repo_override_dir = os.path.join(tmpdir, "owner", "bad-repo")
            os.makedirs(repo_override_dir, exist_ok=True)
            repo_override_path = os.path.join(repo_override_dir, "llm_config.toml")

            with open(base_config_path, "w", encoding="utf-8") as f:
                f.write('[backend]\ndefault = "codex"\n')

            with open(repo_override_path, "w", encoding="utf-8") as f:
                f.write("INVALID TOML CONTENT [[[")

            with pytest.raises(ValueError, match="Error loading repository configuration override"):
                LLMBackendConfiguration.load_from_file(
                    config_path=base_config_path,
                    repo_name="owner/bad-repo",
                )

    def test_backend_config_extra_args_and_environment_properties(self):
        cfg = BackendConfig(name="custom_backend")
        assert cfg.environment is None
        cfg.environment = "env-xyz"
        assert cfg.environment_id == "env-xyz"
        assert cfg.environment == "env-xyz"

        # Extra args attribute fallback
        cfg.extra_args["max_tasks"] = 10
        assert cfg.max_tasks == 10

        with pytest.raises(AttributeError):
            _ = cfg.non_existent_key


class TestGetLLMConfigAndIsolation:
    """Test get_llm_config with active_repo_context and multi-repo isolation."""

    @pytest.fixture(autouse=True)
    def setup_configs(self):
        reset_llm_config()
        set_active_repo_name(None)
        yield
        reset_llm_config()
        set_active_repo_name(None)

    def test_repo_isolation_sequential(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_config_path = os.path.join(tmpdir, "llm_config.toml")
            repo_a_dir = os.path.join(tmpdir, "org", "repo-a")
            repo_b_dir = os.path.join(tmpdir, "org", "repo-b")
            os.makedirs(repo_a_dir, exist_ok=True)
            os.makedirs(repo_b_dir, exist_ok=True)

            with open(base_config_path, "w", encoding="utf-8") as f:
                f.write(
                    """
[backend]
default = "codex"

[backends.codex_cloud]
environment = "base-env"
model = "base-model"
"""
                )

            with open(os.path.join(repo_a_dir, "llm_config.toml"), "w", encoding="utf-8") as f:
                f.write(
                    """
[backends.codex_cloud]
environment = "repo-a-env"
"""
                )

            with open(os.path.join(repo_b_dir, "llm_config.toml"), "w", encoding="utf-8") as f:
                f.write(
                    """
[backends.codex_cloud]
environment = "repo-b-env"
"""
                )

            # Global base lookup
            base_cfg = get_llm_config(config_path=base_config_path)
            assert base_cfg.get_backend_config("codex_cloud").environment == "base-env"

            # Context A
            with active_repo_context("org/repo-a"):
                assert get_active_repo_name() == "org/repo-a"
                cfg_a = get_llm_config(config_path=base_config_path)
                assert cfg_a.get_backend_config("codex_cloud").environment == "repo-a-env"

            # Context B
            with active_repo_context("org/repo-b"):
                assert get_active_repo_name() == "org/repo-b"
                cfg_b = get_llm_config(config_path=base_config_path)
                assert cfg_b.get_backend_config("codex_cloud").environment == "repo-b-env"

            # Back to base
            assert get_active_repo_name() is None
            base_cfg_after = get_llm_config(config_path=base_config_path)
            assert base_cfg_after.get_backend_config("codex_cloud").environment == "base-env"

            # Direct param A
            cfg_a_direct = get_llm_config(repo_name="org/repo-a", config_path=base_config_path)
            assert cfg_a_direct.get_backend_config("codex_cloud").environment == "repo-a-env"


class TestCloudClientsRepoScoping:
    """Test CodexCloudClient and ClaudeRoutineClient repo-scoped configuration loading."""

    @pytest.fixture(autouse=True)
    def setup_configs(self):
        reset_llm_config()
        set_active_repo_name(None)
        yield
        reset_llm_config()
        set_active_repo_name(None)

    def test_codex_cloud_client_repo_scoped_environment(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_config_path = os.path.join(tmpdir, "llm_config.toml")
            repo_dir = os.path.join(tmpdir, "kitamura-tetsuo", "auto-coder")
            os.makedirs(repo_dir, exist_ok=True)

            with open(base_config_path, "w", encoding="utf-8") as f:
                f.write(
                    """
[backends.codex_cloud]
environment = "env-outliner"
attempts = 1
"""
                )

            with open(os.path.join(repo_dir, "llm_config.toml"), "w", encoding="utf-8") as f:
                f.write(
                    """
[backends.codex_cloud]
environment = "env-autocoder"
attempts = 3
"""
                )

            monkeypatch.setenv("AUTO_CODER_CONFIG_PATH", base_config_path)

            # 1. Base client
            client_base = CodexCloudClient(backend_name="codex_cloud")
            assert client_base.environment_id == "env-outliner"
            assert client_base.attempts == 1

            # 2. Repo-scoped client
            client_repo = CodexCloudClient(backend_name="codex_cloud", repo_name="kitamura-tetsuo/auto-coder")
            assert client_repo.environment_id == "env-autocoder"
            assert client_repo.attempts == 3

    def test_claude_routine_client_repo_scoped(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_config_path = os.path.join(tmpdir, "llm_config.toml")
            repo_dir = os.path.join(tmpdir, "kitamura-tetsuo", "auto-coder")
            os.makedirs(repo_dir, exist_ok=True)

            with open(base_config_path, "w", encoding="utf-8") as f:
                f.write(
                    """
[backends.claude_routine]
claude_code_routine_token = "token-base"
url = "https://base.url"
"""
                )

            with open(os.path.join(repo_dir, "llm_config.toml"), "w", encoding="utf-8") as f:
                f.write(
                    """
[backends.claude_routine]
claude_code_routine_token = "token-autocoder"
url = "https://autocoder.url"
"""
                )

            monkeypatch.setenv("AUTO_CODER_CONFIG_PATH", base_config_path)

            client_base = ClaudeRoutineClient(backend_name="claude_routine")
            assert client_base.token == "token-base"
            assert client_base.url == "https://base.url"

            client_repo = ClaudeRoutineClient(backend_name="claude_routine", repo_name="kitamura-tetsuo/auto-coder")
            assert client_repo.token == "token-autocoder"
            assert client_repo.url == "https://autocoder.url"
