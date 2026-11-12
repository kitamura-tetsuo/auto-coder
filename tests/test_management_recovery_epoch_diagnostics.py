"""Bounded issuer-refusal diagnostics; no native acceptance authority."""

from __future__ import annotations

import errno
import json
import re
from pathlib import Path

import pytest

from auto_coder.logger_config import setup_logger
from auto_coder.management import recovery_epoch as issuer


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
