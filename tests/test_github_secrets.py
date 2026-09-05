"""Regression tests for the production Codex-auth publication boundary."""

import json
from io import StringIO

import httpx
import pytest
from click.testing import CliRunner
from loguru import logger as application_logger
from nacl.encoding import Base64Encoder
from nacl.public import PrivateKey, SealedBox

from auto_coder.cli_commands_main import process_issues
from auto_coder.github_secrets import DEFAULT_SECRET_NAME, SyncOutcome, synchronize_codex_auth_secret
from auto_coder.util.gh_cache import ActionsSecretPermissionError, ActionsSecretPublisher


class RecordingPublisher:
    token = ""
    calls: list[tuple[str, str, str]] = []

    def __init__(self, token: str) -> None:
        type(self).token = token

    def set_repository_secret(self, repository: str, secret_name: str, value: str) -> None:
        type(self).calls.append((repository, secret_name, value))


@pytest.fixture(autouse=True)
def reset_recording_publisher(monkeypatch):
    RecordingPublisher.token = ""
    RecordingPublisher.calls = []
    monkeypatch.setattr("auto_coder.util.gh_cache.ActionsSecretPublisher", RecordingPublisher)


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_documented_config_and_codex_home_publish_exact_complete_content(monkeypatch, tmp_path):
    config = _write_config(
        tmp_path,
        '[github_secrets]\nenabled = true\ntoken = "test-secret-write-token"\n' 'repository = "kitamura-tetsuo/auto-coder"\nsecret_name = "CI_CODEX_LOGIN"\n',
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    content = '{\n  "tokens": {"access_token": "recognizable-auth-marker"}\n}\n'
    (codex_home / "auth.json").write_text(content, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GITHUB_TOKEN", "ordinary-github-token-must-not-be-used")

    result = synchronize_codex_auth_secret(str(config))

    assert result.outcome is SyncOutcome.PUBLISHED
    assert RecordingPublisher.token == "test-secret-write-token"
    assert RecordingPublisher.calls == [("kitamura-tetsuo/auto-coder", "CI_CODEX_LOGIN", content)]
    assert (codex_home / "auth.json").read_text(encoding="utf-8") == content


@pytest.mark.parametrize("body", ["", "[github_secrets]\n", "[github_secrets]\nenabled = false\n"])
def test_absent_or_disabled_configuration_is_noop(tmp_path, body):
    result = synchronize_codex_auth_secret(str(_write_config(tmp_path, body)))
    assert result.outcome is SyncOutcome.DISABLED
    assert RecordingPublisher.calls == []


@pytest.mark.parametrize(
    "body",
    [
        '[github_secrets]\nenabled = true\nrepository = "owner/name"\n',
        '[github_secrets]\nenabled = true\ntoken = "dedicated"\nrepository = "name"\n',
        '[github_secrets]\nenabled = true\ntoken = "dedicated"\nrepository = "owner/name"\nalias = "unsafe"\n',
    ],
)
def test_enabled_configuration_fails_closed_without_exact_required_fields(tmp_path, body):
    result = synchronize_codex_auth_secret(str(_write_config(tmp_path, body)))
    assert result.outcome is SyncOutcome.CONFIGURATION_ERROR
    assert RecordingPublisher.calls == []


@pytest.mark.parametrize("content", [None, "", "[]", "not-json"])
def test_unusable_local_auth_never_replaces_remote_secret(monkeypatch, tmp_path, content):
    config = _write_config(tmp_path, '[github_secrets]\nenabled = true\ntoken = "dedicated"\nrepository = "owner/name"\n')
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    if content is not None:
        (codex_home / "auth.json").write_text(content, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = synchronize_codex_auth_secret(str(config))

    assert result.outcome is SyncOutcome.LOCAL_AUTH_ERROR
    assert result.secret_name == DEFAULT_SECRET_NAME
    assert RecordingPublisher.calls == []


def test_changed_local_auth_is_republished(monkeypatch, tmp_path):
    config = _write_config(tmp_path, '[github_secrets]\nenabled = true\ntoken = "dedicated"\nrepository = "owner/name"\n')
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth = codex_home / "auth.json"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    auth.write_text('{"state":"A"}', encoding="utf-8")
    assert synchronize_codex_auth_secret(str(config)).outcome is SyncOutcome.PUBLISHED
    auth.write_text('{"state":"B"}', encoding="utf-8")
    assert synchronize_codex_auth_secret(str(config)).outcome is SyncOutcome.PUBLISHED
    assert [call[2] for call in RecordingPublisher.calls] == ['{"state":"A"}', '{"state":"B"}']


def test_omitted_secret_name_publishes_to_literal_codex_auth_json(monkeypatch, tmp_path):
    config = _write_config(tmp_path, '[github_secrets]\nenabled = true\ntoken = "dedicated"\nrepository = "owner/name"\n')
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"valid":true}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert synchronize_codex_auth_secret(str(config)).outcome is SyncOutcome.PUBLISHED
    assert RecordingPublisher.calls == [("owner/name", "CODEX_AUTH_JSON", '{"valid":true}')]


def test_successful_publication_diagnostics_never_include_secret_markers(monkeypatch, tmp_path, capsys):
    token_marker = "successful-dedicated-token-marker"
    auth_marker = "successful-local-auth-marker"
    config = _write_config(tmp_path, f'[github_secrets]\nenabled = true\ntoken = "{token_marker}"\nrepository = "owner/name"\n')
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({"token": auth_marker}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    log_output = StringIO()
    sink_id = application_logger.add(log_output, format="{message}")
    try:
        result = synchronize_codex_auth_secret(str(config))
    finally:
        application_logger.remove(sink_id)
    captured = capsys.readouterr()
    diagnostics = log_output.getvalue() + captured.out + captured.err

    assert result.outcome is SyncOutcome.PUBLISHED
    assert token_marker not in diagnostics
    assert auth_marker not in diagnostics


def test_permission_failure_is_sanitized_and_local_file_unchanged(monkeypatch, tmp_path, capsys):
    token_marker = "recognizable-secret-token-marker"
    auth_marker = "recognizable-local-auth-marker"
    config = _write_config(tmp_path, f'[github_secrets]\nenabled = true\ntoken = "{token_marker}"\nrepository = "owner/name"\n')
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth = codex_home / "auth.json"
    original = json.dumps({"token": auth_marker})
    auth.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def reject(*_args, **_kwargs):
        raise ActionsSecretPermissionError(f"rejected {token_marker} {auth_marker}")

    monkeypatch.setattr(RecordingPublisher, "set_repository_secret", reject)
    result = synchronize_codex_auth_secret(str(config))
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert result.outcome is SyncOutcome.PERMISSION_ERROR
    assert token_marker not in output
    assert auth_marker not in output
    assert auth.read_text(encoding="utf-8") == original


def test_rest_publisher_encrypts_value_and_uses_only_dedicated_token(monkeypatch):
    private_key = PrivateKey.generate()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"key_id": "key-1", "key": private_key.public_key.encode(Base64Encoder).decode("ascii")})
        return httpx.Response(201)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("auto_coder.util.gh_cache.httpx.Client", lambda **kwargs: real_client(transport=transport, **kwargs))

    ActionsSecretPublisher("dedicated-marker").set_repository_secret("owner/name", "CODEX_AUTH_JSON", "complete auth content")

    assert [request.method for request in requests] == ["GET", "PUT"]
    assert all(request.headers["authorization"] == "Bearer dedicated-marker" for request in requests)
    payload = json.loads(requests[1].content)
    decrypted = SealedBox(private_key).decrypt(payload["encrypted_value"].encode("ascii"), Base64Encoder).decode("utf-8")
    assert payload["key_id"] == "key-1"
    assert decrypted == "complete auth content"
    assert requests[0].url.path == "/repos/owner/name/actions/secrets/public-key"
    assert requests[1].url.path == "/repos/owner/name/actions/secrets/CODEX_AUTH_JSON"


def test_discovered_config_and_real_publisher_preserve_crlf_bytes(monkeypatch, tmp_path):
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[github_secrets]\nenabled = true\ntoken = "dedicated"\nrepository = "owner/name"\n', encoding="utf-8")
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    original = b'{\r\n  "token": "value"\r\n}\r\n'
    (codex_home / "auth.json").write_bytes(original)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    private_key = PrivateKey.generate()
    put_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"key_id": "key-1", "key": private_key.public_key.encode(Base64Encoder).decode("ascii")})
        put_payload.update(json.loads(request.content))
        return httpx.Response(201)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("auto_coder.util.gh_cache.ActionsSecretPublisher", ActionsSecretPublisher)
    monkeypatch.setattr("auto_coder.util.gh_cache.httpx.Client", lambda **kwargs: real_client(transport=transport, **kwargs))

    result = synchronize_codex_auth_secret()
    decrypted = SealedBox(private_key).decrypt(put_payload["encrypted_value"].encode("ascii"), Base64Encoder)

    assert result.outcome is SyncOutcome.PUBLISHED
    assert decrypted == original
    assert (codex_home / "auth.json").read_bytes() == original


def test_deeply_nested_auth_failure_does_not_block_normal_github_initialization(monkeypatch, tmp_path):
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[github_secrets]\nenabled = true\ntoken = "dedicated"\nrepository = "owner/name"\n', encoding="utf-8")
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    original = ("[" * 10_000 + "0" + "]" * 10_000).encode()
    auth_path = codex_home / "auth.json"
    auth_path.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    class BackendConfig:
        backend_order = ["codex"]
        default_backend = "codex"

        def get_active_backends(self):
            return ["codex"]

    initialized = []

    def stop_after_github_initialization(*args, **kwargs):
        initialized.append((args, kwargs))
        raise RuntimeError("ordinary-github-initialized")

    monkeypatch.setattr("auto_coder.cli_commands_main.get_llm_config", lambda **kwargs: BackendConfig())
    monkeypatch.setattr("auto_coder.cli_commands_main.is_jules_mode_enabled", lambda **kwargs: False)
    monkeypatch.setattr("auto_coder.cli_commands_main.build_models_map", lambda: {})
    monkeypatch.setattr("auto_coder.cli_commands_main.setup_logger", lambda **kwargs: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.setup_progress_footer_logging", lambda: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.start_health_monitoring", lambda: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.get_github_token_or_fail", lambda token: "ordinary-token")
    monkeypatch.setattr("auto_coder.cli_commands_main.check_backend_prerequisites", lambda backends: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.ensure_test_script_or_fail", lambda: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.print_configuration_summary", lambda *args: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.GitHubClient.get_instance", stop_after_github_initialization)

    log_output = StringIO()
    sink_id = application_logger.add(log_output, format="{message}")
    try:
        result = CliRunner().invoke(process_issues, ["--repo", "owner/name"])
    finally:
        application_logger.remove(sink_id)

    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "ordinary-github-initialized"
    assert initialized == [(("ordinary-token",), {"disable_labels": False})]
    assert "local-auth error" in log_output.getvalue()
    assert RecordingPublisher.calls == []
    assert auth_path.read_bytes() == original
