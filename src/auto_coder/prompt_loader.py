"""Utilities for loading and formatting LLM instruction prompts from YAML files."""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any, Dict, Optional

import yaml

from .logger_config import get_logger, log_calls

logger = get_logger(__name__)

DEFAULT_PROMPTS_PATH = Path(__file__).resolve().parent / "prompts.yaml"

_PROMPTS_CACHE: Dict[Path, Dict[str, Any]] = {}


def _resolve_path(path: Optional[str] = None) -> Path:
    """Resolve the prompt configuration path."""
    if path is None:
        return DEFAULT_PROMPTS_PATH
    return Path(path).expanduser().resolve()


def load_prompts(path: Optional[str] = None) -> Dict[str, Any]:
    """Load prompts from YAML file, caching the parsed mapping.

    Requirement: If prompts fail to load, subsequent processing cannot continue, so exit immediately.
    Therefore, this function raises SystemExit as a fatal error.
    """
    resolved = _resolve_path(path)
    cached = _PROMPTS_CACHE.get(resolved)
    if cached is not None:
        return cached

    try:
        with resolved.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        msg = f"Prompt configuration file not found: {resolved}"
        logger.critical(msg)
        raise SystemExit(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"Failed to parse prompt configuration: {resolved}: {exc}"
        logger.critical(msg)
        raise SystemExit(msg) from exc

    if not isinstance(data, dict):
        msg = f"Prompt configuration root must be a mapping: {resolved}"
        logger.critical(msg)
        raise SystemExit(msg)

    _PROMPTS_CACHE[resolved] = data
    return data


def clear_prompt_cache() -> None:
    """Clear the prompt cache (useful for tests)."""
    _PROMPTS_CACHE.clear()


def _traverse(prompts: Dict[str, Any], key: str) -> Any:
    """Traverse nested dictionaries using dot-separated keys."""
    current: Any = prompts
    for segment in key.split("."):
        if not isinstance(current, dict):
            raise KeyError(f"Prompt path '{key}' does not resolve to a mapping")
        if segment not in current:
            raise KeyError(f"Prompt '{key}' not found in configuration")
        current = current[segment]
    return current


def get_prompt_template(key: str, path: Optional[str] = None) -> str:
    """Return the raw prompt template string for the given key.

    Referencing an undefined key has no meaning even if processing continues, so exit immediately with SystemExit as a fatal error.
    """
    prompts = load_prompts(path)
    try:
        template = _traverse(prompts, key)
    except KeyError as exc:
        # Fail-fast for missing prompt keys to avoid continuing with undefined instructions
        msg = str(exc)
        logger.critical(msg)
        raise SystemExit(msg) from exc
    if not isinstance(template, str):
        raise ValueError(f"Prompt '{key}' must map to a string template")
    return template


@log_calls
def render_prompt(
    key: str,
    *,
    path: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> str:
    """Render a prompt template identified by key with provided parameters."""
    template_str = get_prompt_template(key, path=path)
    template = Template(template_str)

    params: Dict[str, Any] = {}
    if data:
        params.update(data)
    params.update(kwargs)

    safe_params = {name: "" if value is None else str(value) for name, value in params.items()}
    try:
        rendered_prompt = template.safe_substitute(safe_params)

        # Load prompts to get header
        prompts = load_prompts(path)
        header = prompts.get("header", "")

        # Prepend header to the rendered prompt
        if header:
            return f"{header.rstrip()}\n\n{rendered_prompt}"
        else:
            return rendered_prompt
    except Exception as exc:  # pragma: no cover - Template handles placeholders gracefully
        logger.error(f"Failed to render prompt '{key}': {exc}")
        raise
