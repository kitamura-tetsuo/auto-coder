from unittest.mock import patch

from src.auto_coder.cli import qwen_help_has_flags


@patch("subprocess.run")
def test_qwen_help_probe_handles_trailing_space_and_text(mock_run):
    class Dummy:
        returncode = 0
        stdout = """
Usage: qwen [OPTIONS]

Options:
  --prompt  TEXT    Provide prompt non-interactively
  --model   TEXT    Specify model name
  --help             Show this message and exit.
"""
        stderr = ""
    mock_run.return_value = Dummy()

    # Require short flags, but only long forms appear
    assert qwen_help_has_flags(["-p", "-m"]) is True


@patch("subprocess.run")
def test_qwen_help_probe_handles_table_style(mock_run):
    class Dummy:
        returncode = 0
        stdout = """
Usage: qwen [OPTIONS]

Options:
  | Flag              | Description                      |
  |-------------------|----------------------------------|
  | -p, --prompt TEXT | Provide prompt non-interactively |
  | -m, --model  TEXT | Specify model name               |
  | --help            | Show this message and exit.      |
"""
        stderr = ""
    mock_run.return_value = Dummy()

    assert qwen_help_has_flags(["--prompt", "--model"]) is True




@patch("subprocess.run")
def test_qwen_help_probe_handles_unknown_flags_and_separators(mock_run):
    class Dummy:
        returncode = 0
        stdout = """
Usage: qwen [OPTIONS]

Options:
  ------------------
  --foo, --bar       Unknown flag for future use
  -p,  --prompt  TEXT   Provide prompt non-interactively

  --model TEXT          Specify model name
  ------------------
  --help                Show this message and exit.
"""
        stderr = ""
    mock_run.return_value = Dummy()

    assert qwen_help_has_flags(["-p", "--model"]) is True
