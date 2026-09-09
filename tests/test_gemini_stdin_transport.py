import hashlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.gemini_client import GeminiClient


def _backend(options=None, options_for_noedit=None):
    backend = MagicMock()
    backend.model = "gemini-test"
    backend.options = options or []
    backend.options_for_noedit = options_for_noedit or []
    backend.usage_markers = []
    backend.api_key = None
    backend.validate_required_options.return_value = []
    backend.replace_placeholders.return_value = {
        "options": backend.options,
        "options_for_noedit": backend.options_for_noedit,
    }
    return backend


def _client(monkeypatch, tmp_path, backend):
    executable = tmp_path / "agy_probe.py"
    executable.write_text(
        """import hashlib, json, os, sys
if '--version' in sys.argv:
    print('agy probe 1.0')
    raise SystemExit(0)
payload = sys.stdin.buffer.read()
print(json.dumps({'argv': sys.argv[1:], 'size': len(payload),
                  'digest': hashlib.sha256(payload).hexdigest(),
                  'payload_in_env': any(payload.decode() == value for value in os.environ.values())}))
""",
        encoding="utf-8",
    )
    config = MagicMock()
    config.get_backend_config.return_value = backend
    monkeypatch.setenv("AUTOCODER_GEMINI_CLI", f"{sys.executable} {executable}")
    with patch("src.auto_coder.gemini_client.get_llm_config", return_value=config):
        return GeminiClient(backend_name="agy-alias")


@pytest.mark.parametrize("size,is_noedit", [(256 * 1024 + 17, False), (2 * 1024 * 1024 + 31, True)])
def test_production_adapter_delivers_exact_large_prompt_through_stdin(monkeypatch, tmp_path, size, is_noedit):
    backend = _backend(
        options=["--model", "gemini-test", "--output-format", "text"],
        options_for_noedit=["--model", "gemini-test", "--agent", "planner"],
    )
    client = _client(monkeypatch, tmp_path, backend)
    client.set_extra_args(["--conversation", "conversation-123"])
    prompt = "  日本語🙂 @value '-x'\r\n" + ("q" * size) + "  "
    prepared = prompt.replace("@", "\\@").strip()

    response = json.loads(client._run_llm_cli(prompt, is_noedit=is_noedit))

    expected_options = backend.options_for_noedit if is_noedit else backend.options
    assert response["argv"] == expected_options + ["--conversation", "conversation-123", "--input-format", "text"]
    assert response["size"] == len(prepared.encode("utf-8"))
    assert response["digest"] == hashlib.sha256(prepared.encode("utf-8")).hexdigest()
    assert response["payload_in_env"] is False


@pytest.mark.parametrize(
    "options",
    [
        ["--input-format", "stream-json"],
        ["--input-format=json"],
        ["--input-format"],
        ["-p", "prompt"],
        ["--print=prompt"],
        ["--prompt", "prompt"],
    ],
)
def test_competing_or_non_text_input_is_rejected_before_launch(options):
    with pytest.raises(ValueError, match="Antigravity"):
        GeminiClient._ensure_stdin_input_mode(["agy", *options])


def test_explicit_text_input_mode_is_normalized_once():
    assert GeminiClient._ensure_stdin_input_mode(["agy", "--input-format=text", "--output-format", "json"]) == [
        "agy",
        "--output-format",
        "json",
        "--input-format",
        "text",
    ]
