"""Production-boundary regressions for retiring the GitHub processing label."""

from unittest.mock import MagicMock

from click.testing import CliRunner

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.cli_commands_main import process_issues
from src.auto_coder.label_manager import LabelManager


def test_processing_scope_is_label_independent_and_never_mutates_github():
    """Identical lifecycle work is admitted with and without the historical label."""
    outcomes = []
    for labels in ([], [{"name": "@auto-coder"}]):
        github = MagicMock()
        with LabelManager(github, "owner/repo", 1793, known_labels=labels) as admitted:
            outcomes.append(bool(admitted))
        github.has_label.assert_not_called()
        github.try_add_labels.assert_not_called()
        github.add_labels.assert_not_called()
        github.remove_labels.assert_not_called()

    assert outcomes == [True, True]


def test_lock_only_configuration_is_removed_but_general_label_switch_remains():
    config = AutomationConfig(env_override=False)
    assert not hasattr(config, "CHECK_LABELS")
    assert not hasattr(config, "AUTO_CODER_LABEL")
    assert config.DISABLE_LABELS is False

    help_text = CliRunner().invoke(process_issues, ["--help"]).output
    assert "--check-labels" not in help_text
    assert "--disable-labels" in help_text
