"""Render the production decomposition-review prompt for Promptfoo cases."""

from pathlib import Path
from string import Template

import yaml


def render(context):
    """Load the production YAML template and substitute one Promptfoo case."""
    variables = context["vars"]
    prompt_path = Path(__file__).resolve().parents[3] / "src/auto_coder/prompts.yaml"
    template = yaml.safe_load(prompt_path.read_text(encoding="utf-8"))["issue"]["adversarial_decomposition_analysis"]
    return Template(template).substitute({key: str(value) for key, value in variables.items()})
