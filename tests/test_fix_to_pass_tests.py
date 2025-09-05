"""
Tests for fix-to-pass-tests mode.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from src.auto_coder.automation_engine import AutomationEngine


def _cmd_result(success=True, stdout="", stderr="", returncode=0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_engine_fix_to_pass_tests_no_edits_raises(mock_github_client, mock_gemini_client):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)

    # First run: tests fail
    engine._run_local_tests = Mock(return_value={
        'success': False,
        'output': 'E AssertionError: expected 1 == 2',
        'errors': '',
        'return_code': 1,
    })
    # LLM returns a message but does not change files
    engine._apply_workspace_test_fix = Mock(return_value="Applied change")
    # git add succeeds
    engine.cmd.run_command = Mock(side_effect=[
        _cmd_result(True),  # git add .
    ])
    # commit reports nothing to commit -> treat as no edits
    engine._commit_with_message = Mock(return_value=_cmd_result(False, stdout='nothing to commit'))

    try:
        engine.fix_to_pass_tests(max_attempts=1)
        assert False, "Expected RuntimeError when no edits were made"
    except RuntimeError as e:
        assert "no edits" in str(e).lower()


def test_engine_fix_to_pass_tests_succeeds_after_edit(mock_github_client, mock_gemini_client):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)

    # Sequence: fail first, then pass
    test_runs = [
        {'success': False, 'output': 'E AssertionError: x', 'errors': '', 'return_code': 1},
        {'success': True, 'output': '1 passed', 'errors': '', 'return_code': 0},
    ]
    def _run_local_tests_seq():
        return test_runs.pop(0)
    engine._run_local_tests = Mock(side_effect=_run_local_tests_seq)

    # LLM applies a fix
    engine._apply_workspace_test_fix = Mock(return_value="Applied fix to make tests pass")

    # git add . succeeds; commit succeeds
    engine.cmd.run_command = Mock(return_value=_cmd_result(True))
    engine._commit_with_message = Mock(return_value=_cmd_result(True))

    result = engine.fix_to_pass_tests(max_attempts=3)
    assert result['success'] is True
    assert result['attempts'] == 2
    assert any('passed' in m.lower() for m in result['messages'])


def test_cli_fix_to_pass_tests_invokes_engine(monkeypatch):
    from click.testing import CliRunner
    from src.auto_coder.cli import fix_to_pass_tests_command

    # Patch clients and engine inside CLI
    from src.auto_coder import cli as cli_mod
    dummy_engine = Mock()
    dummy_engine.fix_to_pass_tests.return_value = {'success': True, 'attempts': 1, 'messages': []}
    monkeypatch.setattr(cli_mod, 'AutomationEngine', Mock(return_value=dummy_engine))
    monkeypatch.setattr(cli_mod, 'GitHubClient', Mock())
    monkeypatch.setattr(cli_mod, 'CodexClient', Mock())
    monkeypatch.setattr(cli_mod, 'GeminiClient', Mock())
    monkeypatch.setattr(cli_mod, 'check_codex_cli_or_fail', Mock(return_value=None))
    monkeypatch.setattr(cli_mod, 'check_gemini_cli_or_fail', Mock(return_value=None))

    runner = CliRunner()
    res = runner.invoke(fix_to_pass_tests_command, [
        '--backend', 'codex',
        '--max-attempts', '5'
    ])

    assert res.exit_code == 0
    dummy_engine.fix_to_pass_tests.assert_called_once()
