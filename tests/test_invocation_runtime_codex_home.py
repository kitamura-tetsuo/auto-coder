from pathlib import Path
from types import SimpleNamespace

from src.auto_coder.utils import _prepare_invocation_runtime


def _context(invocation_id: str) -> SimpleNamespace:
    boundary = SimpleNamespace(binding=SimpleNamespace(invocation_id=invocation_id))
    return SimpleNamespace(boundary=boundary, supervisor=SimpleNamespace())


def test_private_runtime_creates_codex_home_without_caller_credentials(tmp_path: Path) -> None:
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir()
    environment = {"AUTO_CODER_RUNTIME_ROOT": str(tmp_path / "runtime"), "HOME": str(caller_home)}
    (tmp_path / "runtime" / "local-invocations").mkdir(parents=True)

    runtime = _prepare_invocation_runtime(_context("inv-a"), environment)  # type: ignore[arg-type]

    assert Path(environment["CODEX_HOME"]).is_dir()
    assert Path(environment["CODEX_HOME"]).parent == runtime / "home"
    assert list(Path(environment["CODEX_HOME"]).iterdir()) == []


def test_private_runtime_copies_codex_credentials_into_codex_home(tmp_path: Path) -> None:
    caller_home = tmp_path / "caller-home"
    (caller_home / ".codex").mkdir(parents=True)
    (caller_home / ".codex" / "auth.json").write_text("{}")
    environment = {"AUTO_CODER_RUNTIME_ROOT": str(tmp_path / "runtime"), "HOME": str(caller_home)}
    (tmp_path / "runtime" / "local-invocations").mkdir(parents=True)

    _prepare_invocation_runtime(_context("inv-b"), environment)  # type: ignore[arg-type]

    assert (Path(environment["CODEX_HOME"]) / "auth.json").read_text() == "{}"
