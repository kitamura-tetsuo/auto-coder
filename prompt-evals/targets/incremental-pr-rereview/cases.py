"""Expose the counted semantic corpus to Promptfoo as individual test cases."""

from pathlib import Path

import yaml


def generate_tests(config=None):
    """Return the exact cases counted by the selective evaluation runner."""
    document = yaml.safe_load((Path(__file__).parent / "cases/policy.yaml").read_text(encoding="utf-8"))
    return document["tests"]
