"""Render the shared production parent/child contract-boundary policy for semantic evaluation."""

from pathlib import Path

import yaml


def render(context):
    prompts = yaml.safe_load((Path(__file__).resolve().parents[3] / "src/auto_coder/prompts.yaml").read_text(encoding="utf-8"))
    policies = prompts["policies"]
    return f"{policies['parent_child_contract_boundary']}\n\nSCENARIO:\n{context['vars']['scenario']}\n\nReturn only the requested JSON decision."
