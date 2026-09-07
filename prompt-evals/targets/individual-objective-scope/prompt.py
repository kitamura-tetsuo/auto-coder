"""Render the production individual-review prompt for Promptfoo cases."""

from auto_coder.prompt_loader import render_prompt


def render(context):
    variables = context["vars"]
    return render_prompt("issue.adversarial_specification_analysis", **variables)
