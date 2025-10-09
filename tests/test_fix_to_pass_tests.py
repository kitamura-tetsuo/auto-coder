"""
Tests for fix-to-pass-tests mode.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.auto_coder import fix_to_pass_tests_runner as fix_to_pass_tests_runner_module
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.fix_to_pass_tests_runner import WorkspaceFixResult
from src.auto_coder.backend_manager import BackendManager


def _cmd_result(success=True, stdout="", stderr="", returncode=0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_engine_fix_to_pass_tests_small_change_retries_without_commit(
    mock_github_client, mock_gemini_client, monkeypatch
):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)

    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'check_for_updates_and_restart', Mock(return_value=None))

    # Always failing output to simulate <10% change across retries
    engine._run_local_tests = Mock(return_value={
        'success': False,
        'output': 'E AssertionError: expected 1 == 2',
        'errors': '',
        'return_code': 1,
    })
    # LLM returns a message but effectively makes no meaningful change
    engine._apply_workspace_test_fix = Mock(return_value=WorkspaceFixResult(
        summary='Applied change but minimal impact',
        raw_response='Applied change but minimal impact',
        backend='codex',
        model='gpt-4',
    ))

    # Run with max_attempts=1: it should retry once after LLM and stop without commit, not raise
    result = engine.fix_to_pass_tests(max_attempts=1)
    assert result['success'] is False
    assert result['attempts'] == 2  # first run + post-fix retry
    # Ensure message indicates skipping commit and retrying due to small change
    assert any('skipping commit and retrying' in m.lower() for m in result['messages'])


def test_engine_fix_to_pass_tests_succeeds_after_edit(mock_github_client, mock_gemini_client):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=False)

    # Mock the fix_to_pass_tests function in test_runner.py to return a successful result
    with patch('src.auto_coder.automation_engine.fix_to_pass_tests') as mock_fix_to_pass_tests:
        mock_fix_to_pass_tests.return_value = {
            'success': True,
            'attempts': 2,
            'messages': ['Local tests passed on attempt 2']
        }

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


def test_fix_to_pass_tests_creates_llm_logs(monkeypatch, tmp_path):
    from src.auto_coder.automation_config import AutomationConfig

    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'check_for_updates_and_restart', Mock(return_value=None))

    config = AutomationConfig()

    failing_result = {
        'success': False,
        'output': '',
        'errors': 'AssertionError',
        'return_code': 1,
        'command': 'bash scripts/test.sh',
        'test_file': 'tests/test_sample.py::test_fail',
    }
    post_fix_success = {
        'success': True,
        'output': 'ok',
        'errors': '',
        'return_code': 0,
        'command': 'bash scripts/test.sh',
        'test_file': None,
    }

    run_results = [failing_result, post_fix_success, post_fix_success]

    def fake_run_local_tests(cfg, test_file=None):
        result = run_results.pop(0).copy()
        # Preserve explicit test_file when the stub wants to echo the requested target
        result.setdefault('test_file', test_file)
        return result

    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'run_local_tests', fake_run_local_tests)

    fix_response = WorkspaceFixResult(
        summary='Applied fix',
        raw_response='Detailed LLM output',
        backend='codex',
        model='gpt-4',
    )
    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'apply_workspace_test_fix', Mock(return_value=fix_response))

    class DummyCmd:
        DEFAULT_TIMEOUTS = {'test': 60}

        def __init__(self):
            self.calls = []

        def run_command(self, command, timeout=None):
            self.calls.append((tuple(command), timeout))
            return SimpleNamespace(success=True, stdout='', stderr='', returncode=0)

    dummy_cmd = DummyCmd()
    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'cmd', dummy_cmd)

    monkeypatch.setattr(
        fix_to_pass_tests_runner_module,
        'generate_commit_message_via_llm',
        Mock(return_value='Auto-Coder: log test'),
    )

    summary = fix_to_pass_tests_runner_module.fix_to_pass_tests(config, dry_run=False, max_attempts=2, llm_backend_manager=Mock())

    assert summary['success'] is True

    csv_path = tmp_path / '.auto-coder' / 'fix_to_pass_tests_summury.csv'
    assert csv_path.exists()
    rows = csv_path.read_text(encoding='utf-8').strip().splitlines()
    assert len(rows) == 2
    assert rows[0].split(',')[0] == 'current_test_file'
    assert 'tests/test_sample.py::test_fail' in rows[1]
    assert ',codex,' in rows[1]
    assert 'gpt-4' in rows[1]

    log_dir = tmp_path / '.auto-coder' / 'log'
    log_files = list(log_dir.glob('*.txt'))
    assert len(log_files) == 1
    log_name = log_files[0].name
    sanitized_test = fix_to_pass_tests_runner_module._sanitize_for_filename(
        fix_to_pass_tests_runner_module._normalize_test_file('tests/test_sample.py::test_fail'),
        default='tests',
    )
    assert sanitized_test in log_name
    assert 'codex' in log_name
    assert 'gpt-4' in log_name
    assert 'Detailed LLM output' in log_files[0].read_text(encoding='utf-8')


def test_fix_to_pass_tests_records_backend_manager_metadata(monkeypatch, tmp_path):
    from src.auto_coder.automation_config import AutomationConfig
    import csv

    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'check_for_updates_and_restart', Mock(return_value=None))

    config = AutomationConfig()

    failing_file = 'e2e/new/als-alias-keyboard-navigation.spec.ts::should_fail'
    failing_result = {
        'success': False,
        'output': '',
        'errors': 'Error: boom',
        'return_code': 1,
        'command': 'bash scripts/test.sh',
        'test_file': failing_file,
    }
    targeted_success = {
        'success': True,
        'output': 'ok',
        'errors': '',
        'return_code': 0,
        'command': 'bash scripts/test.sh',
        'test_file': failing_file,
    }
    final_success = {
        'success': True,
        'output': 'ok',
        'errors': '',
        'return_code': 0,
        'command': 'bash scripts/test.sh',
        'test_file': None,
    }

    run_results = [failing_result, targeted_success, final_success]

    def fake_run_local_tests(cfg, test_file=None):
        result = run_results.pop(0).copy()
        result.setdefault('test_file', test_file)
        return result

    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'run_local_tests', fake_run_local_tests)

    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'render_prompt', Mock(return_value='PROMPT'))
    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'extract_important_errors', Mock(return_value='ERR BLOCK'))

    class DummyCmd:
        DEFAULT_TIMEOUTS = {'test': 60}

        def __init__(self):
            self.calls = []

        def run_command(self, command, timeout=None):
            self.calls.append((tuple(command), timeout))
            return SimpleNamespace(success=True, stdout='', stderr='', returncode=0)

    dummy_cmd = DummyCmd()
    monkeypatch.setattr(fix_to_pass_tests_runner_module, 'cmd', dummy_cmd)
    monkeypatch.setattr(
        fix_to_pass_tests_runner_module,
        'generate_commit_message_via_llm',
        Mock(return_value='Auto-Coder: log test'),
    )

    calls: list[tuple[str, str]] = []

    class DummyLLM:
        def __init__(self, name: str, model_name: str) -> None:
            self.name = name
            self.model_name = model_name

        def _run_llm_cli(self, prompt: str) -> str:
            calls.append((self.name, prompt))
            return f'{self.name}:{prompt}'

        def switch_to_default_model(self) -> None:
            pass

    manager = BackendManager(
        default_backend='codex',
        default_client=DummyLLM('codex', 'gpt-4o-mini'),
        factories={
            'codex': lambda: DummyLLM('codex', 'gpt-4o-mini'),
            'gemini': lambda: DummyLLM('gemini', 'gemini-2.0-pro'),
        },
        order=['codex', 'gemini'],
    )

    summary = fix_to_pass_tests_runner_module.fix_to_pass_tests(
        config,
        dry_run=False,
        max_attempts=3,
        llm_backend_manager=manager,
    )

    assert summary['success'] is True
    assert any('Local tests passed' in msg for msg in summary['messages'])

    csv_path = tmp_path / '.auto-coder' / 'fix_to_pass_tests_summury.csv'
    assert csv_path.exists()
    with csv_path.open('r', encoding='utf-8') as handle:
        rows = list(csv.reader(handle))

    assert rows[0] == ['current_test_file', 'backend', 'model', 'timestamp']
    assert rows[1][0] == failing_file
    assert rows[1][1] == 'codex'
    assert rows[1][2] == 'gpt-4o-mini'

    log_dir = tmp_path / '.auto-coder' / 'log'
    log_files = list(log_dir.glob('*.txt'))
    assert len(log_files) == 1
    log_name = log_files[0].name
    assert 'codex' in log_name
    assert 'gpt-4o-mini' in log_name
    content = log_files[0].read_text(encoding='utf-8')
    assert 'codex:PROMPT' in content

    assert calls == [('codex', 'PROMPT')]
    assert any(cmd == ('git', 'add', '.') for cmd, _ in dummy_cmd.calls)


def test_generate_commit_message_via_llm_with_code_blocks():
    """Test that generate_commit_message_via_llm correctly extracts messages from code blocks."""
    from src.auto_coder.fix_to_pass_tests_runner import generate_commit_message_via_llm

    # Mock BackendManager
    mock_manager = Mock()

    # Test case 1: Message wrapped in code blocks
    mock_manager._run_llm_cli.return_value = "```\nFix test failure in authentication module\n```"
    result = generate_commit_message_via_llm(mock_manager)
    assert result == "Auto-Coder: Fix test failure in authentication module"

    # Test case 2: Message with multiple lines in code blocks (should take first line)
    mock_manager._run_llm_cli.return_value = "```\nFix test failure\nAdditional details\n```"
    result = generate_commit_message_via_llm(mock_manager)
    assert result == "Auto-Coder: Fix test failure"

    # Test case 3: Message without code blocks
    mock_manager._run_llm_cli.return_value = "Fix test failure without code blocks"
    result = generate_commit_message_via_llm(mock_manager)
    assert result == "Auto-Coder: Fix test failure without code blocks"

    # Test case 4: Message with quotes (quotes should be stripped, but backticks in content are preserved)
    mock_manager._run_llm_cli.return_value = '```\n"Fix `test` failure"\n```'
    result = generate_commit_message_via_llm(mock_manager)
    assert result == "Auto-Coder: Fix `test` failure"

    # Test case 5: Long message should be truncated to 72 characters
    long_message = "A" * 100
    mock_manager._run_llm_cli.return_value = f"```\n{long_message}\n```"
    result = generate_commit_message_via_llm(mock_manager)
    assert len(result) <= 72 + len("Auto-Coder: ")
    assert result.startswith("Auto-Coder: ")

    # Test case 6: Empty response
    mock_manager._run_llm_cli.return_value = ""
    result = generate_commit_message_via_llm(mock_manager)
    assert result == ""

    # Test case 7: None manager
    result = generate_commit_message_via_llm(None)
    assert result == ""

    # Test case 8: Code blocks with only markers (edge case)
    mock_manager._run_llm_cli.return_value = "```\n```"
    result = generate_commit_message_via_llm(mock_manager)
    assert result == ""

    # Test case 9: Code blocks in the middle of response
    mock_manager._run_llm_cli.return_value = "Here is the message:\n```\nFix authentication bug\n```\nEnd of message"
    result = generate_commit_message_via_llm(mock_manager)
    assert result == "Auto-Coder: Fix authentication bug"

    # Test case 10: Code blocks with language identifier
    mock_manager._run_llm_cli.return_value = "```bash\nFix shell script error\n```"
    result = generate_commit_message_via_llm(mock_manager)
    assert result == "Auto-Coder: Fix shell script error"

    # Test case 11: Multiple code blocks (should use first one)
    mock_manager._run_llm_cli.return_value = "```\nFirst message\n```\nSome text\n```\nSecond message\n```"
    result = generate_commit_message_via_llm(mock_manager)
    assert result == "Auto-Coder: First message"


def test_run_local_tests_detects_stability_issue(monkeypatch, tmp_path):
    """Test that run_local_tests detects when a test fails in full suite but passes in isolation."""
    from src.auto_coder.automation_config import AutomationConfig
    from src.auto_coder.fix_to_pass_tests_runner import run_local_tests
    from src.auto_coder import fix_to_pass_tests_runner as fix_module

    monkeypatch.chdir(tmp_path)
    config = AutomationConfig()

    # Mock extract_first_failed_test to return a test file
    monkeypatch.setattr(fix_module, 'extract_first_failed_test', Mock(return_value='tests/test_example.py'))

    # Mock CommandExecutor to simulate:
    # 1. Full suite fails
    # 2. Isolated test passes
    call_count = [0]

    def mock_run_command(cmd_list, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # Full suite fails
            return _cmd_result(
                success=False,
                stdout='FAILED tests/test_example.py::test_something',
                stderr='',
                returncode=1
            )
        else:
            # Isolated test passes
            return _cmd_result(
                success=True,
                stdout='PASSED tests/test_example.py::test_something',
                stderr='',
                returncode=0
            )

    class DummyCmd:
        DEFAULT_TIMEOUTS = {'test': 60}
        run_command = staticmethod(mock_run_command)

    monkeypatch.setattr(fix_module, 'cmd', DummyCmd())

    result = run_local_tests(config)

    # Should detect stability issue
    assert result['stability_issue'] is True
    assert result['test_file'] == 'tests/test_example.py'
    assert result['success'] is False  # Overall result is still failure
    assert 'full_suite_result' in result
    assert result['full_suite_result']['success'] is False


def test_apply_test_stability_fix(monkeypatch, tmp_path):
    """Test that apply_test_stability_fix calls LLM with correct prompt."""
    from src.auto_coder.automation_config import AutomationConfig
    from src.auto_coder.fix_to_pass_tests_runner import apply_test_stability_fix
    from src.auto_coder import fix_to_pass_tests_runner as fix_module

    monkeypatch.chdir(tmp_path)
    config = AutomationConfig()

    # Mock render_prompt
    mock_render = Mock(return_value="Test stability fix prompt")
    monkeypatch.setattr(fix_module, 'render_prompt', mock_render)

    # Create mock backend manager
    class DummyLLM:
        def __init__(self):
            self.backend = 'codex'
            self.model_name = 'gpt-4o-mini'

        def _run_llm_cli(self, prompt):
            return "Fixed test isolation issue"

        def get_last_backend_and_model(self):
            return self.backend, self.model_name

    manager = DummyLLM()

    full_suite_result = {
        'success': False,
        'output': 'FAILED tests/test_example.py',
        'errors': 'AssertionError',
    }

    isolated_result = {
        'success': True,
        'output': 'PASSED tests/test_example.py',
        'errors': '',
    }

    result = apply_test_stability_fix(
        config,
        'tests/test_example.py',
        full_suite_result,
        isolated_result,
        manager,
        dry_run=False,
    )

    # Verify render_prompt was called with correct template
    mock_render.assert_called_once()
    call_args = mock_render.call_args
    assert call_args[0][0] == 'tests.test_stability_fix'
    assert 'test_file' in call_args[1]
    assert call_args[1]['test_file'] == 'tests/test_example.py'

    # Verify result
    assert result.summary == "Fixed test isolation issue"
    assert result.backend == 'codex'
    assert result.model == 'gpt-4o-mini'


def test_fix_to_pass_tests_handles_stability_issue(monkeypatch, tmp_path):
    """Test that fix_to_pass_tests properly handles stability issues."""
    from src.auto_coder.automation_config import AutomationConfig
    from src.auto_coder.fix_to_pass_tests_runner import fix_to_pass_tests, WorkspaceFixResult
    from src.auto_coder import fix_to_pass_tests_runner as fix_module
    from src.auto_coder.backend_manager import BackendManager

    monkeypatch.chdir(tmp_path)
    config = AutomationConfig()

    monkeypatch.setattr(fix_module, 'check_for_updates_and_restart', Mock(return_value=None))

    # Mock run_local_tests to return stability issue on first call, then success
    call_count = [0]

    def mock_run_local_tests(cfg, test_file=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: stability issue detected
            return {
                'success': False,
                'output': 'PASSED tests/test_example.py',
                'errors': '',
                'return_code': 0,
                'command': 'bash scripts/test.sh tests/test_example.py',
                'test_file': 'tests/test_example.py',
                'stability_issue': True,
                'full_suite_result': {
                    'success': False,
                    'output': 'FAILED tests/test_example.py',
                    'errors': 'AssertionError',
                    'return_code': 1,
                    'command': 'bash scripts/test.sh',
                },
            }
        elif call_count[0] == 2:
            # After stability fix: still failing (post-fix check)
            return {
                'success': False,
                'output': 'FAILED tests/test_example.py',
                'errors': 'Still failing',
                'return_code': 1,
                'command': 'bash scripts/test.sh tests/test_example.py',
                'test_file': 'tests/test_example.py',
                'stability_issue': False,
            }
        else:
            # Final check: success
            return {
                'success': True,
                'output': 'All tests passed',
                'errors': '',
                'return_code': 0,
                'command': 'bash scripts/test.sh',
                'test_file': None,
                'stability_issue': False,
            }

    monkeypatch.setattr(fix_module, 'run_local_tests', mock_run_local_tests)

    # Mock apply_test_stability_fix
    mock_stability_fix = Mock(return_value=WorkspaceFixResult(
        summary='Fixed test isolation',
        raw_response='Fixed test isolation',
        backend='codex',
        model='gpt-4o-mini',
    ))
    monkeypatch.setattr(fix_module, 'apply_test_stability_fix', mock_stability_fix)

    # Mock git commands
    class DummyCmd:
        DEFAULT_TIMEOUTS = {'test': 60}

        def run_command(self, cmd_list, timeout=None):
            return _cmd_result(success=True, stdout='', stderr='', returncode=0)

    monkeypatch.setattr(fix_module, 'cmd', DummyCmd())

    # Mock git_commit_with_retry
    monkeypatch.setattr(fix_module, 'git_commit_with_retry', Mock(return_value=_cmd_result(success=True)))

    # Mock generate_commit_message_via_llm
    monkeypatch.setattr(fix_module, 'generate_commit_message_via_llm', Mock(return_value='Auto-Coder: Fix test stability'))

    # Mock cleanup_llm_task_file
    monkeypatch.setattr(fix_module, 'cleanup_llm_task_file', Mock())

    # Create backend manager
    class DummyLLM:
        def __init__(self):
            self.backend = 'codex'
            self.model_name = 'gpt-4o-mini'

        def _run_llm_cli(self, prompt):
            return "Fixed"

        def get_last_backend_and_model(self):
            return self.backend, self.model_name

        def switch_to_default_backend(self):
            pass

    manager = BackendManager(
        default_backend='codex',
        default_client=DummyLLM(),
        factories={'codex': lambda: DummyLLM()},
        order=['codex'],
    )

    result = fix_to_pass_tests(config, dry_run=False, max_attempts=5, llm_backend_manager=manager)

    # Verify stability fix was called
    assert mock_stability_fix.call_count == 1

    # Verify the result contains stability issue message
    assert any('stability issue' in msg.lower() for msg in result['messages'])

    # Eventually succeeds
    assert result['success'] is True
