"""Shared assertions for the fail-closed Chromium CI installation policy."""

from collections.abc import Mapping


def assert_conditional_chromium_install(step: Mapping[str, object]) -> None:
    """Require a real launch probe and dependency repair without success masking."""
    assert set(step) == {"name", "run"}
    script = step["run"]
    assert isinstance(script, str)
    commands = [line.strip() for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert commands == [
        "uv run playwright install chromium",
        "if ! uv run python scripts/browser_preflight.py; then",
        "uv run playwright install-deps chromium",
        "fi",
    ]
