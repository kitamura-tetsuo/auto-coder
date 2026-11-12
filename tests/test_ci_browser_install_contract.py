"""Negative tests ensure browser prerequisite checks cannot become optional."""

import pytest

from tests.support.ci_browser_install import assert_conditional_chromium_install

VALID = "\n".join(
    [
        "uv run playwright install chromium",
        "if ! uv run python scripts/browser_preflight.py; then",
        "  uv run playwright install-deps chromium",
        "fi",
    ]
)


def test_accepts_probed_dependency_fallback():
    assert_conditional_chromium_install({"name": "Install Chromium", "run": VALID})


@pytest.mark.parametrize(
    "command",
    [
        "uv run playwright install chromium",
        VALID.replace("if ! uv run python scripts/browser_preflight.py; then", "if true; then"),
        VALID.replace("uv run playwright install-deps chromium", "true"),
        VALID.replace("uv run playwright install-deps chromium", "uv run playwright install-deps chromium || true"),
        VALID + "\nexit 0",
    ],
)
def test_refuses_missing_or_masked_browser_dependencies(command):
    with pytest.raises(AssertionError):
        assert_conditional_chromium_install({"name": "Install Chromium", "run": command})


def test_refuses_step_level_success_masking():
    with pytest.raises(AssertionError):
        assert_conditional_chromium_install({"name": "Install Chromium", "run": VALID, "continue-on-error": True})
