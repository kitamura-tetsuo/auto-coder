"""Render the repository-owned ChatGPT adjudication prompt for evaluation."""

from pathlib import Path


def render(context):
    prompt = (Path(__file__).resolve().parents[3] / "docs/prompts/review-adjudication-chatgpt.md").read_text(encoding="utf-8")
    return f"{prompt}\n\nSCENARIO:\n{context['vars']['scenario']}\n\nReturn only JSON with keys action, verdict, directive, and reason."
