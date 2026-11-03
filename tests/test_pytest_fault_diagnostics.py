"""Real-process oracles for the scheduled diagnostic path, not test selection."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace

import pytest

from tests.pytest_fault_diagnostics import ScheduledTraceback, _SafeFaultHandler, pytest_configure

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
ROOT = Path(__file__).resolve().parents[1]


def _subject(tmp_path: Path, code: str) -> subprocess.CompletedProcess[str]:
    # This isolated pytest process is the diagnostic plugin's test subject.
    # Repository qualification itself still uses target scripts/test.sh.
    source = f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n" + textwrap.dedent(code)
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    return subprocess.run([sys.executable, "-I", "-c", source], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)


def test_actual_pytest_dump_avoids_unsafe_native_watchdog(tmp_path):
    result = _subject(
        tmp_path,
        r"""
        import faulthandler
        from pathlib import Path
        import pytest
        from tests import pytest_fault_diagnostics as plugin
        def forbidden(*args, **kwargs):
            raise RuntimeError("unsafe native watchdog")
        faulthandler.dump_traceback_later = forbidden
        initial = faulthandler.is_enabled()
        Path("test_subject.py").write_text(
            '\n'
            'import faulthandler\n'
            'from threading import Event, Thread\n'
            'def test_subject():\n'
            '    assert faulthandler.is_enabled()\n'
            '    stop = Event()\n'
            '    def diagnostic_worker():\n'
            '        stop.wait(2)\n'
            '    worker = Thread(target=diagnostic_worker)\n'
            '    worker.start()\n'
            '    try:\n'
            '        Event().wait(0.08)\n'
            '    finally:\n'
            '        stop.set()\n'
            '        worker.join(1)\n'
            '        assert not worker.is_alive()'
        )
        status = pytest.main(["test_subject.py", "-s", "-o", "faulthandler_timeout=0.01"], plugins=[plugin])
        assert faulthandler.is_enabled() == initial
        raise SystemExit(status)
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Scheduled Python-owned traceback" in result.stderr
    assert "diagnostic_worker" in result.stderr
    assert "test_subject" in result.stderr
    assert "unsafe native watchdog" not in result.stdout + result.stderr


def test_actual_pytest_exit_on_timeout_is_preserved(tmp_path):
    result = _subject(
        tmp_path,
        """
        from pathlib import Path
        import pytest
        from tests import pytest_fault_diagnostics as plugin
        Path("test_subject.py").write_text("from threading import Event\\ndef test_subject():\\n    Event().wait(2)\\n    print('TEST_FINISHED')\\n")
        raise SystemExit(pytest.main(["test_subject.py", "-s", "-o", "faulthandler_timeout=0.02", "-o", "faulthandler_exit_on_timeout=true"], plugins=[plugin]))
        """,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Timeout" in result.stderr
    assert "Scheduled Python-owned traceback" not in result.stderr
    assert "TEST_FINISHED" not in result.stdout


def test_exit_deadline_does_not_wait_for_native_code_to_release_gil(tmp_path):
    result = _subject(
        tmp_path,
        """
        from pathlib import Path
        import pytest
        from tests import pytest_fault_diagnostics as plugin
        Path("test_subject.py").write_text(
            "import ctypes, time\\nfrom pathlib import Path\\n"
            "def test_subject():\\n"
            "    Path('entered.monotonic').write_text(str(time.monotonic()))\\n"
            "    ctypes.PyDLL(None).sleep(3)\\n"
            "    print('TEST_FINISHED')\\n"
        )
        raise SystemExit(pytest.main(["test_subject.py", "-s", "-o", "faulthandler_timeout=0.05", "-o", "faulthandler_exit_on_timeout=true"], plugins=[plugin]))
        """,
    )
    elapsed = time.monotonic() - float((tmp_path / "entered.monotonic").read_text())
    assert result.returncode == 1, result.stdout + result.stderr
    assert elapsed < 2, (elapsed, result.stdout, result.stderr)
    assert "TEST_FINISHED" not in result.stdout


@pytest.mark.parametrize("disabled", [False, True])
def test_zero_or_explicitly_disabled_configuration_has_no_timer(tmp_path, disabled):
    result = _subject(
        tmp_path,
        f"""
        import faulthandler
        from pathlib import Path
        import pytest
        from tests import pytest_fault_diagnostics as plugin
        def forbidden(*args, **kwargs):
            raise RuntimeError("unexpected scheduled diagnostic")
        faulthandler.dump_traceback_later = forbidden
        Path("test_subject.py").write_text("def test_subject():\\n    assert True\\n")
        args = ["test_subject.py", "-s"]
        args += ["-p", "no:faulthandler"] if {disabled!r} else ["-o", "faulthandler_timeout=0"]
        raise SystemExit(pytest.main(args, plugins=[plugin]))
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Scheduled Python-owned traceback" not in result.stderr


def test_native_fatal_handler_remains_enabled(tmp_path):
    result = _subject(
        tmp_path,
        """
        from pathlib import Path
        import pytest
        from tests import pytest_fault_diagnostics as plugin
        Path("test_subject.py").write_text("import os, faulthandler\\ndef test_subject():\\n    assert faulthandler.is_enabled()\\n    os.abort()\\n")
        raise SystemExit(pytest.main(["test_subject.py", "-s"], plugins=[plugin]))
        """,
    )
    assert result.returncode != 0
    assert "Fatal Python error" in result.stderr
    assert "test_subject" in result.stderr


def test_cancelled_timer_cannot_write_to_a_reused_descriptor(tmp_path):
    result = _subject(
        tmp_path,
        """
        from pathlib import Path
        from threading import Event, enumerate as threads
        from tests.pytest_fault_diagnostics import ScheduledTraceback
        first = open("first.log", "wb")
        descriptor = first.fileno()
        timer = ScheduledTraceback(0.03, descriptor)
        timer.start()
        timer.stop()
        timer.stop()
        first.close()
        with open("second.log", "wb") as second:
            assert second.fileno() == descriptor
            second.write(b"second")
            second.flush()
            Event().wait(0.08)
        assert Path("first.log").read_bytes() == b""
        assert Path("second.log").read_bytes() == b"second"
        assert not any(t.name == "pytest-safe-traceback" for t in threads())
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("boundary", ["pytest_enter_pdb", "pytest_exception_interact"])
def test_interactive_boundaries_cancel_owned_timer(tmp_path, boundary):
    adapter = _SafeFaultHandler(ModuleType("original"))
    with (tmp_path / "diagnostics.log").open("wb") as output:
        timer = ScheduledTraceback(0.03, output.fileno())
        adapter.current = timer
        timer.start()
        getattr(adapter, boundary)()
        adapter.cancel()
        Event().wait(0.06)
        assert adapter.current is None
    assert (tmp_path / "diagnostics.log").read_bytes() == b""


def test_unsupported_adapter_refuses_before_plugin_replacement():
    replaced = []
    manager = SimpleNamespace(get_plugin=lambda _name: object(), unregister=lambda _plugin: replaced.append(True))
    with pytest.raises(pytest.UsageError, match="unsupported"):
        pytest_configure(SimpleNamespace(pluginmanager=manager, stash={}))
    assert replaced == []


def test_diagnostic_failure_remains_visible(tmp_path):
    with (tmp_path / "closed.log").open("wb") as output:
        timer = ScheduledTraceback(0.01, output.fileno())
    with pytest.raises(OSError):
        timer.start()


def test_error_during_dump_is_not_silently_ignored(tmp_path, monkeypatch):
    failure = OSError("controlled diagnostic write failure")

    def fail(**_kwargs):
        raise failure

    monkeypatch.setattr("tests.pytest_fault_diagnostics.faulthandler.dump_traceback", fail)
    with (tmp_path / "failed-dump.log").open("wb") as output:
        timer = ScheduledTraceback(0.01, output.fileno())
        timer.start()
        Event().wait(0.05)
        with pytest.raises(RuntimeError, match="traceback failed") as caught:
            timer.stop()
        assert caught.value.__cause__ is failure
