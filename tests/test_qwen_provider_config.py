from __future__ import annotations

from auto_coder.qwen_provider_config import load_qwen_provider_configs
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
