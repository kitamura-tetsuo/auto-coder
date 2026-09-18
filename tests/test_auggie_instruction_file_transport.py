import hashlib
import json
import os
import stat
import textwrap
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.auggie_client import AuggieClient


def _harness(tmp_path: Path) -> Path:
    script = tmp_path / "auggie-harness"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import hashlib, json, os, stat, sys, time
            if sys.argv[1:] == ["--version"]:
                print("1.0")
                raise SystemExit
            marker = os.environ["AUGGIE_RESULT"]
            args = sys.argv[1:]
            path = args[args.index("--instruction-file") + 1]
            time.sleep(float(os.environ.get("AUGGIE_DELAY", "0")))
            data = open(path, "rb").read()
            result = {
                "argv": args,
                "digest": hashlib.sha256(data).hexdigest(),
                "mode": stat.S_IMODE(os.stat(path).st_mode),
                "regular": stat.S_ISREG(os.stat(path).st_mode),
                "path": path,
                "stdin": sys.stdin.read(),
            }
            open(marker, "w").write(json.dumps(result))
            print("STREAMED-RESULT")
            """
        )
    )
    script.chmod(0o755)
    return script


def _client(executable: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options=None) -> AuggieClient:
    monkeypatch.setenv("AUTOCODER_AUGGIE_CLI", str(executable))
    monkeypatch.setenv("AUTO_CODER_AUGGIE_USAGE_DIR", str(tmp_path / "usage"))
    backend = Mock()
    backend.model = "GPT-5"
    backend.options = options if options is not None else ["--model", "GPT-5"]
    backend.options_for_noedit = []
    backend.usage_markers = []
    backend.validate_required_options.return_value = []
    backend.replace_placeholders.return_value = {
        "options": backend.options,
        "options_for_noedit": [],
        "options_for_resume": [],
    }
    config = Mock()
    config.get_backend_config.return_value = backend
    with patch("src.auto_coder.auggie_client.get_llm_config", return_value=config):
        return AuggieClient("auggie")


@pytest.mark.parametrize("size", [32, 256 * 1024 + 31, 2 * 1024 * 1024 + 17])
def test_instruction_file_transports_exact_prepared_bytes(tmp_path, monkeypatch, size):
    executable = _harness(tmp_path)
    result_path = tmp_path / "result.json"
    monkeypatch.setenv("AUGGIE_RESULT", str(result_path))
    client = _client(executable, monkeypatch, tmp_path)
    prompt = "  @'$(touch nope)\r\n雪" + ("x" * size) + "  "

    assert client._run_llm_cli(prompt) == "STREAMED-RESULT"

    result = json.loads(result_path.read_text())
    expected = prompt.replace("@", "\\@").strip().encode()
    assert result["digest"] == hashlib.sha256(expected).hexdigest()
    assert result["argv"].count("--instruction-file") == 1
    assert result["argv"].count("--print") == 1
    assert prompt not in result["argv"]
    assert result["stdin"] == ""
    assert result["regular"] is True
    assert result["mode"] == 0o600
    assert not Path(result["path"]).exists()
    assert not Path(result["path"]).is_relative_to(Path.cwd())


def test_competing_instruction_option_fails_before_launch(tmp_path, monkeypatch):
    executable = _harness(tmp_path)
    result_path = tmp_path / "result.json"
    monkeypatch.setenv("AUGGIE_RESULT", str(result_path))
    client = _client(executable, monkeypatch, tmp_path, ["--instruction=wrong"])

    with pytest.raises(RuntimeError, match="conflicts with"):
        client._run_llm_cli("task")

    assert not result_path.exists()


def test_unsafe_configured_temp_directory_is_bypassed(tmp_path, monkeypatch):
    executable = _harness(tmp_path)
    result_path = tmp_path / "result.json"
    unsafe = Path.cwd() / ".agent-tmp"
    unsafe.mkdir(exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(unsafe))
    monkeypatch.setattr("src.auto_coder.auggie_client.tempfile.tempdir", None)
    monkeypatch.setenv("AUGGIE_RESULT", str(result_path))
    client = _client(executable, monkeypatch, tmp_path)

    client._run_llm_cli("task")

    result = json.loads(result_path.read_text())
    assert not Path(result["path"]).is_relative_to(Path.cwd())
