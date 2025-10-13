"""Helpers for loading Qwen provider fallback configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

try:  # Python 3.11+ ships with tomllib
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - fallback for older interpreters
    import tomli as tomllib  # type: ignore[import]

from .logger_config import get_logger

logger = get_logger(__name__)


CONFIG_OVERRIDE_ENV = "AUTO_CODER_QWEN_CONFIG"
CONFIG_DIR_ENV = "AUTO_CODER_CONFIG_DIR"
CONFIG_FILENAME = "qwen-providers.toml"


@dataclass
class QwenProviderConfig:
    """Representation of a single Qwen provider option."""

    name: str
    api_key: str
    base_url: Optional[str]
    model: Optional[str]
    description: str


_PROVIDER_DEFAULTS = {
    "modelstudio": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-coder-plus",
        "description": "Alibaba Cloud ModelStudio compatible endpoint",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "qwen/qwen3-coder:free",
        "description": "OpenRouter free tier compatible endpoint",
    },
}


def _resolve_config_path() -> Path:
    """Return the path to the Qwen provider configuration file."""

    override = os.environ.get(CONFIG_OVERRIDE_ENV)
    if override:
        return Path(override).expanduser()

    base_dir = os.environ.get(CONFIG_DIR_ENV)
    if base_dir:
        return Path(base_dir).expanduser() / CONFIG_FILENAME

    return Path.home() / ".auto-coder" / CONFIG_FILENAME


def _iter_provider_entries(
    raw_providers: Iterable[dict],
) -> Iterable[QwenProviderConfig]:
    """Convert raw provider dicts into ``QwenProviderConfig`` objects."""

    for entry in raw_providers:
        name_raw = entry.get("name")
        if not name_raw:
            logger.warning("Skipping Qwen provider without a name: %s", entry)
            continue

        name = str(name_raw).strip()
        if not name:
            logger.warning("Skipping Qwen provider with empty name: %s", entry)
            continue

        api_key = entry.get("api_key")
        if not api_key:
            logger.info(
                "Skipping Qwen provider '%s' because no api_key was provided", name
            )
            continue

        defaults = _PROVIDER_DEFAULTS.get(name.lower(), {})
        base_url = entry.get("base_url") or defaults.get("base_url")
        model = entry.get("model") or defaults.get("model")
        description = entry.get("description") or defaults.get("description") or name

        yield QwenProviderConfig(
            name=name,
            api_key=str(api_key),
            base_url=str(base_url) if base_url else None,
            model=str(model) if model else None,
            description=str(description),
        )


def load_qwen_provider_configs() -> List[QwenProviderConfig]:
    """Load configured Qwen providers from disk.

    The configuration format is a TOML file with the following structure::

        [[qwen.providers]]
        name = "modelstudio"
        api_key = "dashscope-..."
        # base_url/model are optional; defaults from the known provider table apply.

    Providers are returned in the order defined in the file. Entries without an
    ``api_key`` are skipped because they cannot be invoked.
    """

    path = _resolve_config_path()
    if not path.exists():
        logger.debug("Qwen provider config not found at %s", path)
        return []

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error("Failed to read Qwen provider config %s: %s", path, exc)
        return []

    providers = data.get("qwen", {}).get("providers", [])
    if not isinstance(providers, list):
        logger.error(
            "Invalid Qwen provider config at %s: expected list under qwen.providers",
            path,
        )
        return []

    return list(_iter_provider_entries(providers))
