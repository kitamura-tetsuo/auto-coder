from __future__ import annotations

from src.auto_coder.provider_constants import DEFAULT_CODEX_ARGS
from src.auto_coder.qwen_provider_config import QwenProviderConfig, build_provider_metadata_entries, load_qwen_provider_configs
from tests.support.env import patch_environment


def test_load_qwen_provider_configs_defaults(tmp_path) -> None:
    config_path = tmp_path / "qwen-providers.toml"
    config_path.write_text(
        """
        [[qwen.providers]]
        name = "modelstudio"
        api_key = "dashscope-xyz"

        [[qwen.providers]]
        name = "openrouter"
        api_key = "openrouter-123"
        model = "custom-model"
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    env = {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
    with patch_environment(env):
        providers = load_qwen_provider_configs()

    assert [p.name for p in providers] == ["modelstudio", "openrouter"]
    modelstudio = providers[0]
    assert modelstudio.base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert modelstudio.model == "qwen3-coder-plus"

    openrouter = providers[1]
    assert openrouter.base_url == "https://openrouter.ai/api/v1"
    assert openrouter.model == "custom-model"


def test_load_qwen_provider_configs_skips_missing_key(tmp_path) -> None:
    config_path = tmp_path / "qwen-providers.toml"
    config_path.write_text(
        """
        [[qwen.providers]]
        name = "modelstudio"

        [[qwen.providers]]
        name = "openrouter"
        api_key = "valid"
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    env = {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
    with patch_environment(env):
        providers = load_qwen_provider_configs()

    assert len(providers) == 1
    assert providers[0].name == "openrouter"


def test_build_provider_metadata_entries_preserves_order_and_defaults() -> None:
    providers = [
        QwenProviderConfig(
            name="ModelStudio",
            api_key="dashscope-xyz",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            model="qwen3-coder-plus",
            description="ModelStudio",
        ),
        QwenProviderConfig(
            name="OpenRouter",
            api_key="openrouter-123",
            base_url=None,
            model=None,
            description="OpenRouter",
        ),
    ]

    entries = build_provider_metadata_entries(providers)
    assert list(entries.keys()) == ["legacy-modelstudio", "legacy-openrouter"]

    first = entries["legacy-modelstudio"]
    assert first["command"] == "codex"
    assert first["args"] == list(DEFAULT_CODEX_ARGS)
    assert first["OPENAI_API_KEY"] == "dashscope-xyz"
    assert first["OPENAI_BASE_URL"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert first["OPENAI_MODEL"] == "qwen3-coder-plus"

    second = entries["legacy-openrouter"]
    assert second["OPENAI_API_KEY"] == "openrouter-123"
    assert "OPENAI_BASE_URL" not in second
    assert "OPENAI_MODEL" not in second


def test_build_provider_metadata_entries_handles_duplicate_names() -> None:
    providers = [
        QwenProviderConfig("Example Provider", "key-1", None, None, "Example1"),
        QwenProviderConfig("Example Provider", "key-2", None, None, "Example2"),
    ]

    entries = build_provider_metadata_entries(providers, prefix="custom")
    assert list(entries.keys()) == ["custom-example-provider", "custom-example-provider-2"]
    assert entries["custom-example-provider"]["OPENAI_API_KEY"] == "key-1"
    assert entries["custom-example-provider-2"]["OPENAI_API_KEY"] == "key-2"
