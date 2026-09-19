"""Render the shipped Jules initial-instruction guidance for semantic evaluation.

This target is advisory, opt-in Promptfoo evidence for how a real model
reading the shipped `cloud_provider_instructions.jules.initial` text is
expected to *behave*, not a deterministic assertion. Deterministic delivery,
literal-text, and clause-presence regressions live separately in
`tests/test_cloud_provider_instructions.py` and
`tests/test_cloud_provider_instructions_integration.py`.
"""

from pathlib import Path

import yaml


def render(context):
    prompts = yaml.safe_load((Path(__file__).resolve().parents[3] / "src/auto_coder/prompts.yaml").read_text(encoding="utf-8"))
    guidance = prompts["cloud_provider_instructions"]["jules"]["initial"]
    return f"{guidance}\n\nTASK/SESSION CONTEXT:\n{context['vars']['scenario']}\n\nReturn only the requested JSON decision."
