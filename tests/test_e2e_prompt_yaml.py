"""E2E-style tests ensuring prompts are sourced from YAML templates."""

from textwrap import dedent

import pytest

from src.auto_coder import prompt_loader
from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _create_pr_analysis_prompt


@pytest.fixture
def sample_pr(sample_pr_data):
    data = dict(sample_pr_data)
    data["body"] = "Example body text for YAML prompt test."
    data["user"] = {"login": "octocat"}
    # Remove labels to avoid label-based prompt selection in this test
    data["labels"] = []
    return data


def test_pr_prompt_uses_yaml_template(tmp_path, sample_pr):
    """Customizing the YAML template should affect generated prompts end-to-end."""
    custom_yaml = tmp_path / "prompts.yaml"
    custom_yaml.write_text(
        dedent(
            """
            pr:
              action: |
                CUSTOM DIRECTIVE
                Repository: $repo_name
                Number: $pr_number
            """
        ),
        encoding="utf-8",
    )

    prompt_loader.clear_prompt_cache()
    original_path = prompt_loader.DEFAULT_PROMPTS_PATH
    prompt_loader.DEFAULT_PROMPTS_PATH = custom_yaml
    try:
        prompt = _create_pr_analysis_prompt(
            repo_name="owner/repo",
            pr_data=sample_pr,
            pr_diff="diff-data",
            config=AutomationConfig(),
        )
    finally:
        prompt_loader.DEFAULT_PROMPTS_PATH = original_path
        prompt_loader.clear_prompt_cache()

    assert "CUSTOM DIRECTIVE" in prompt
    assert "owner/repo" in prompt
    assert str(sample_pr["number"]) in prompt
