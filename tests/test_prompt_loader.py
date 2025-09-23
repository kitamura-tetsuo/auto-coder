"""Tests for prompt loader utilities."""

import pytest

from src.auto_coder.prompt_loader import (
    DEFAULT_PROMPTS_PATH,
    clear_prompt_cache,
    get_prompt_template,
    render_prompt,
)


@pytest.fixture
def temp_prompt_file(tmp_path):
    path = tmp_path / 'prompts.yaml'
    path.write_text('category:\n  message: "Hello $name!"\n', encoding='utf-8')
    return path


def test_render_prompt_with_custom_path(temp_prompt_file):
    clear_prompt_cache()
    result = render_prompt('category.message', path=str(temp_prompt_file), name='World')
    assert result == 'Hello World!'


def test_get_prompt_template_uses_cache(temp_prompt_file):
    clear_prompt_cache()
    # First load caches the file
    first = get_prompt_template('category.message', path=str(temp_prompt_file))
    assert 'Hello $name!' in first

    # Overwrite file; cached version should still be returned
    temp_prompt_file.write_text('category:\n  message: "Changed"\n', encoding='utf-8')
    cached = get_prompt_template('category.message', path=str(temp_prompt_file))
    assert cached == first

    clear_prompt_cache()
    refreshed = get_prompt_template('category.message', path=str(temp_prompt_file))
    assert refreshed == 'Changed'


def test_default_prompt_file_exists():
    path = DEFAULT_PROMPTS_PATH
    assert path.exists(), f"Default prompt file missing at {path}"
