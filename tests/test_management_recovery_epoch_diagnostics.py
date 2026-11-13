"""Bounded issuer-refusal diagnostics; no native acceptance authority."""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.logger_config import setup_logger
from auto_coder.management import recovery_containment as containment
from auto_coder.management import recovery_diagnostics as diagnostics
from auto_coder.management import recovery_epoch as issuer
from auto_coder.management.recovery_containment_model import RecoveryContainmentUnavailable, RecoveryResources


def _failure() -> ValueError:
    # The code location is safe metadata; its directory, exception arguments,
    # attached filename and frame locals are deliberately private test data.
    source = "def fail():\n    local_credential = 'private-local-credential'\n    raise FileNotFoundError(2, 'private-error-message', '/private/source-token.txt')\n"
    namespace = {}
    exec(compile(source, "/private/code-directory/observed_component.py", "exec"), namespace)
    try:
        try:
            namespace["fail"]()
        except OSError as cause:
            raise ValueError("fixed issuer refusal") from cause
    except ValueError as error:
        return error


def test_refusal_causes_retain_only_code_identity_and_integer_errno() -> None:
    assert issuer._refusal_causes is diagnostics.refusal_causes
    error = _failure()
    rows = issuer._refusal_causes(error)
    assert rows[0] == {"type": "ValueError", "file": Path(__file__).name, "function": "_failure", "line": error.__traceback__.tb_lineno}
    assert rows[1] == {"type": "FileNotFoundError", "errno": errno.ENOENT, "file": "observed_component.py", "function": "fail", "line": 3}
    assert "private" not in json.dumps(rows) and "fixed issuer refusal" not in json.dumps(rows)
    assert all(set(row) <= {"type", "errno", "file", "function", "line"} for row in rows)


def test_refusal_cause_chain_is_limited_to_eight_entries() -> None:
    error = ValueError("private-deep-message")
    for _ in range(40):
        outer = RuntimeError("private-outer-message")
        outer.__cause__ = error
        error = outer
    assert issuer._refusal_causes(error) == [{"type": "RuntimeError"}] * 8


@pytest.mark.parametrize("self_cycle", [False, True])
def test_refusal_cause_cycles_are_finite(self_cycle: bool) -> None:
    first, second = ValueError("private-first"), OSError(errno.EIO, "private-second")
    first.__cause__ = first if self_cycle else second
    second.__cause__ = first
    assert issuer._refusal_causes(first) == ([{"type": "ValueError"}] if self_cycle else [{"type": "ValueError"}, {"type": "OSError", "errno": errno.EIO}])


@pytest.mark.parametrize("number", ["private-errno", True, None])
def test_refusal_causes_ignore_noninteger_errno(number: object) -> None:
    error = OSError("private-argument")
    error.errno = number
    assert issuer._refusal_causes(error) == [{"type": "OSError"}]


def test_refusal_causes_never_format_exception_objects_or_unsafe_code_metadata() -> None:
    class SensitiveFailure(ValueError):
        def __str__(self) -> str:
            pytest.fail("causal metadata must not format the exception")

    try:
        exec(compile("raise error", "/private/path/invalid filename.py", "exec"), {"error": SensitiveFailure("private-argument")})
    except SensitiveFailure as error:
        assert issuer._refusal_causes(error) == [{"type": "SensitiveFailure"}]


def test_cli_refusal_preserves_exit_and_emits_safe_causes_to_stdout_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "issuer.log"

    def configure(**kwargs: object) -> None:
        kwargs["log_file"] = str(log)
        setup_logger(**kwargs)

    def refuse() -> None:
        raise _failure()

    monkeypatch.setattr(issuer, "setup_logger", configure)
    monkeypatch.setattr(issuer, "run_issuer", refuse)
    assert issuer.main([]) == 2
    issuer.logger.complete()
    standard = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    persisted = log.read_text()
    for output in (standard, persisted):
        assert "Recovery epoch unavailable; preserved evidence is not repaired: fixed issuer refusal" in output
        messages = [line.partition("Recovery epoch refusal cause ")[2] for line in output.splitlines() if "Recovery epoch refusal cause " in line]
        assert len(messages) == 2
        assert [line.partition(": ")[0] for line in messages] == ["1", "2"]
        rows = [json.loads(line.partition(": ")[2]) for line in messages]
        assert rows[1] == {"type": "FileNotFoundError", "errno": errno.ENOENT, "file": "observed_component.py", "function": "fail", "line": 3}
        assert "private" not in output
    issuer.logger.remove()


@pytest.mark.parametrize("action", ["observe", "verify"])
def test_containment_cli_preserves_masked_refusal_and_exports_safe_nested_causes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str) -> None:
    log = tmp_path / "containment.log"
    resources = tmp_path / "resources.json"
    resources.write_text(json.dumps(asdict(RecoveryResources(42, "12345", "00000000-0000-4000-8000-000000000001", (), (), ()))))
    arguments = ["recovery_containment", action, "--enrollment", "component-observation", "--resources", str(resources)]
    if action == "verify":
        previous = tmp_path / "previous.json"
        previous.write_text("{}")
        arguments.extend(("--previous", str(previous)))

    def configure(**kwargs: object) -> None:
        kwargs["log_file"] = str(log)
        setup_logger(**kwargs)

    def masked(*_args: object) -> None:
        try:
            raise _failure()
        except ValueError as cause:
            raise RecoveryContainmentUnavailable("the original namespace pin cannot be retained") from cause

    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(containment, "setup_logger", configure)
    monkeypatch.setattr(containment, action, masked)
    assert containment.main() == 2
    containment.logger.complete()
    standard = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    result = json.loads(standard.splitlines()[-1])
    assert result["schema"] == "current-recovery-containment-v1" and result["status"] == "unavailable"
    assert result["reason"] == "the original namespace pin cannot be retained"
    assert all(result[name] is False for name in ("grant_issued", "history_asserted", "admission_created"))
    assert len(result["causes"]) == 3
    assert result["causes"][-1] == {"type": "FileNotFoundError", "errno": errno.ENOENT, "file": "observed_component.py", "function": "fail", "line": 3}
    assert diagnostics.safe_refusal_causes(result["causes"]) == result["causes"]
    for output in (standard, log.read_text()):
        assert "Current recovery containment unavailable: the original namespace pin cannot be retained" in output
        messages = [line.partition("Current recovery containment refusal cause ")[2] for line in output.splitlines() if "Current recovery containment refusal cause " in line]
        assert [json.loads(line.partition(": ")[2]) for line in messages] == result["causes"]
        assert "private" not in output and "fixed issuer refusal" not in output
    containment.logger.remove()


def test_safe_subprocess_causes_are_a_detached_copy_of_the_exact_metadata() -> None:
    rows = diagnostics.refusal_causes(_failure())
    parsed = diagnostics.safe_refusal_causes(json.loads(json.dumps(rows)))
    assert parsed == rows
    parsed[-1]["line"] = 100
    assert rows[-1]["line"] == 3


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        "private-data",
        [{"type": "ValueError"}] * 9,
        ["private-data"],
        [{"type": "ValueError", "message": "private-data"}],
        [{"type": "invalid type"}],
        [{"type": "ValueError", "errno": True}],
        [{"type": "ValueError", "errno": "private-data"}],
        [{"type": "ValueError", "file": "/private/observed.py", "function": "fail", "line": 3}],
        [{"type": "ValueError", "file": "observed.py", "function": "private argument", "line": 3}],
        [{"type": "ValueError", "file": "observed.py", "function": "fail", "line": True}],
        [{"type": "ValueError", "file": "observed.py", "function": "fail", "line": 0}],
        [{"type": "ValueError", "file": "observed.py"}],
        [{"type": "V" * 97}],
    ],
)
def test_subprocess_causes_reject_oversized_or_nonmetadata_fields(value: object) -> None:
    assert diagnostics.safe_refusal_causes(value) == []


@pytest.mark.usefixtures("_use_real_commands")
@pytest.mark.parametrize("unavailable", [False, True])
def test_containment_cli_keeps_json_last_with_real_asynchronous_logging(tmp_path: Path, unavailable: bool) -> None:
    resources = tmp_path / "resources.json"
    resources.write_text(json.dumps(asdict(RecoveryResources(42, "12345", "00000000-0000-4000-8000-000000000001", (), (), ()))))
    log = tmp_path / "queued.log"
    program = r"""
import sys
import time
from dataclasses import dataclass
from auto_coder.logger_config import setup_logger
from auto_coder.management import recovery_containment as cli

resources, logfile, unavailable = sys.argv[1:]

class DelayedStandardOutput:
    def write(self, text):
        time.sleep(0.002)
        return sys.stdout.write(text)
    def flush(self):
        return sys.stdout.flush()

def configure(**kwargs):
    setup_logger(stream=DelayedStandardOutput(), log_file=logfile)
    assert all(handler._enqueue for handler in cli.logger._core.handlers.values())

@dataclass
class ComponentReport:
    status: str = "observed-current-recovery-containment"
    grant_issued: bool = False
    history_asserted: bool = False
    admission_created: bool = False

def observe(*args):
    for index in range(100):
        cli.logger.warning("Queued component observation {}", index)
    if unavailable == "yes":
        try:
            raise FileNotFoundError(2, "private-inner-message", "/private/source-token")
        except OSError as cause:
            raise cli.RecoveryContainmentUnavailable("fixed masked refusal") from cause
    return ComponentReport()

cli.setup_logger = configure
cli.observe = observe
sys.argv = ["recovery_containment", "observe", "--enrollment", "component", "--resources", resources]
raise SystemExit(cli.main())
"""
    environment = {name: value for name, value in os.environ.items() if name not in {"PYTEST_CURRENT_TEST", "PYTEST_RUNNER", "_PYTEST_CURRENT_TEST", "LLM_LOGGING_DISABLED"}}
    environment["AUTO_CODER_LOG_FILE"] = str(log)
    completed = subprocess.run([sys.executable, "-c", program, str(resources), str(log), "yes" if unavailable else "no"], capture_output=True, text=True, timeout=30, env=environment)
    assert completed.returncode == (2 if unavailable else 0), completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["status"] == ("unavailable" if unavailable else "observed-current-recovery-containment")
    assert all(result[name] is False for name in ("grant_issued", "history_asserted", "admission_created"))
    assert "Queued component observation 99" in completed.stdout and "Queued component observation 99" in log.read_text()
    assert "private" not in completed.stdout and "private" not in log.read_text()
    if unavailable:
        assert result["causes"][-1] == {"type": "FileNotFoundError", "errno": 2}
