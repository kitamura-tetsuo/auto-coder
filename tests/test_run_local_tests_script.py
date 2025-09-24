from types import SimpleNamespace

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder import test_runner as tr


def _res(success=True, stdout="", stderr="", returncode=0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_run_local_tests_uses_script_for_single_file(monkeypatch, tmp_path):
    # Arrange: create a dummy test file path that exists
    test_file = tmp_path / "tests" / "dummy_test.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# dummy")

    calls = []

    def fake_run_command(cmd, timeout=None, cwd=None, check_success=True):
        calls.append(cmd)
        return _res(success=True, stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(tr.cmd, "run_command", fake_run_command)

    cfg = AutomationConfig()
    # Act
    result = tr.run_local_tests(cfg, test_file=str(test_file))

    # Assert
    assert result["success"] is True
    # First (and only) call should be the script with the file argument
    assert calls[0][:2] == ["bash", cfg.TEST_SCRIPT_PATH]
    assert calls[0][2] == str(test_file)


def test_run_local_tests_uses_script_for_all_tests(monkeypatch):
    calls = []

    def fake_run_command(cmd, timeout=None, cwd=None, check_success=True):
        calls.append(cmd)
        return _res(success=True, stdout="all ok", stderr="", returncode=0)

    monkeypatch.setattr(tr.cmd, "run_command", fake_run_command)

    cfg = AutomationConfig()
    result = tr.run_local_tests(cfg)

    assert result["success"] is True
    assert calls[0] == ["bash", cfg.TEST_SCRIPT_PATH]


def test_run_local_tests_reruns_first_failed_via_script(monkeypatch):
    calls = []

    # First run: fail; Second (rerun for first failed): pass
    def fake_run_command(cmd, timeout=None, cwd=None, check_success=True):
        calls.append(cmd)
        if len(calls) == 1:
            return _res(success=False, stdout="failing...", stderr="", returncode=1)
        return _res(success=True, stdout="passed", stderr="", returncode=0)

    monkeypatch.setattr(tr.cmd, "run_command", fake_run_command)
    # Force extractor to return a specific failed test path
    monkeypatch.setattr(tr, "extract_first_failed_test", lambda *_: "e2e/new/fail-example.spec.ts")

    cfg = AutomationConfig()
    result = tr.run_local_tests(cfg)

    assert result["success"] is True
    # First call: run all via script; Second: rerun only the extracted test via script with arg
    assert calls[0] == ["bash", cfg.TEST_SCRIPT_PATH]
    assert calls[1] == ["bash", cfg.TEST_SCRIPT_PATH, "e2e/new/fail-example.spec.ts"]

