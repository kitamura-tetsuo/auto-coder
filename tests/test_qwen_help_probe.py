from unittest.mock import patch

from src.auto_coder.cli_helpers import qwen_help_has_flags


@patch("subprocess.run")
def test_qwen_help_probe_detects_required_flags(mock_run):
    class Dummy:
        returncode = 0
        stdout = """
Usage: qwen [OPTIONS]

Options:
  -p, --prompt TEXT   Provide prompt non-interactively
  -m, --model TEXT    Specify model name
  --help              Show this message and exit.
"""
        stderr = ""

    mock_run.return_value = Dummy()

    assert qwen_help_has_flags(["-p", "-m"]) is True


@patch("subprocess.run")
def test_qwen_help_probe_missing_flag_returns_false(mock_run):
    class Dummy:
        returncode = 0
        stdout = """
Usage: qwen [OPTIONS]

Options:
  -p, --prompt TEXT   Provide prompt non-interactively
  --help              Show this message and exit.
"""
        stderr = ""

    mock_run.return_value = Dummy()

    assert qwen_help_has_flags(["-p", "-m"]) is False


@patch("subprocess.run")
def test_qwen_help_probe_accepts_long_form_for_short_flags(mock_run):
    class Dummy:
        returncode = 0
        stdout = """
Usage: qwen [OPTIONS]

Options:
  --prompt TEXT   Provide prompt non-interactively
  --model TEXT    Specify model name
  --help          Show this message and exit.
"""
        stderr = ""

    mock_run.return_value = Dummy()

    # Require short flags, but help only shows long forms
    assert qwen_help_has_flags(["-p", "-m"]) is True


@patch("subprocess.run")
def test_qwen_help_probe_accepts_short_form_for_long_flags(mock_run):
    class Dummy:
        returncode = 0
        stdout = """
Usage: qwen [OPTIONS]

Options:
  -p TEXT   Provide prompt non-interactively
  -m TEXT    Specify model name
  --help              Show this message and exit.
"""
        stderr = ""

    mock_run.return_value = Dummy()

    # Require long flags, but help only shows short forms
    assert qwen_help_has_flags(["--prompt", "--model"]) is True
