"""Render the task-scoped test policy for advisory semantic evaluation."""

from pathlib import Path

import yaml


def render(context):
    prompts = yaml.safe_load((Path(__file__).resolve().parents[3] / "src/auto_coder/prompts.yaml").read_text(encoding="utf-8"))
    policy = prompts["policies"]["task_scoped_test_execution"]
    return f"{policy}\n\nSCENARIO:\n{context['vars']['scenario']}\n\nReturn only JSON with decision, commands, and reason."
