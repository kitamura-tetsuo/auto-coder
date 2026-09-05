"""Regression tests for the production Codex-auth publication boundary."""

import json

import httpx
import pytest
from nacl.encoding import Base64Encoder
from nacl.public import PrivateKey, SealedBox

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
