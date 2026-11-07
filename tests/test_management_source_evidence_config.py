"""Disposable operator files exercise the production source-only settings path."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.source_evidence_config import REPOSITORY, extension_path, load_extension, prepare
from auto_coder.management.worker_lifecycle import WorkerLifecycle


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.chmod(0o700)
    config = tmp_path / "management.yml"
    config.write_text(f"state_root: {tmp_path / 'oauth'}\npublic_origin: https://manager.example\nfirebase_project_id: test-project\nclients:\n  - client_id: test-client\n    redirect_uris: [https://client.example/callback]\n")
    config.chmod(0o600)
    token = tmp_path / "source_evidence_read"
    token.write_text("disposable-test-value-never-used-for-github")
    token.chmod(0o600)
    return config, token


def install(config: Path, token: Path) -> Path:
    path = extension_path(config)
    path.write_text(json.dumps(prepare(config, REPOSITORY, token)))
    path.chmod(0o600)
    return path


def test_extension_preserves_immutable_yaml_and_retained_state(files: tuple[Path, Path]) -> None:
    config, token = files
    original = config.read_bytes()
    retained = [config.parent / name for name in ["fresh-initialization.json", "oauth.sqlite3", "management-operations.sqlite3", "prior-operations.sqlite3"]]
    for path in retained:
        path.write_bytes(b"retained evidence")
    before = {path: path.read_bytes() for path in retained}
    baseline = load_management_settings(config)
    install(config, token)
    settings = load_management_settings(config)
    assert settings.source_repository == REPOSITORY
    assert settings.source_evidence_token_file == token
    assert WorkerLifecycle(SimpleNamespace(settings=settings))._source() is not None
    assert settings.state_root == baseline.state_root and settings.oauth == baseline.oauth
    assert settings.operation_state_root == baseline.operation_state_root and settings.bootstrap_config_path == baseline.bootstrap_config_path
    assert config.read_bytes() == original
    assert {path: path.read_bytes() for path in retained} == before
    assert hashlib.sha256(original).hexdigest() == json.loads(extension_path(config).read_text())["management_config_sha256"]


@pytest.mark.parametrize("mutation", ["repository", "path", "extra", "missing", "version", "boolean", "hash", "duplicate", "malformed"])
def test_invalid_extension_fails_closed(files: tuple[Path, Path], mutation: str) -> None:
    config, token = files
    path = install(config, token)
    raw = json.loads(path.read_text())
    if mutation == "repository":
        raw["source_repository"] = "other/repo"
    elif mutation == "path":
        raw["source_evidence_token_file"] = "/unrelated/credential"
    elif mutation == "extra":
        raw["state_root"] = "/replacement-oauth"
    elif mutation == "missing":
        del raw["source_repository"]
    elif mutation == "version":
        raw["schema_version"] = 2
    elif mutation == "boolean":
        raw["schema_version"] = True
    elif mutation == "hash":
        raw["management_config_sha256"] = "0" * 64
    data = json.dumps(raw)
    if mutation == "duplicate":
        data = data[:-1] + ', "schema_version": 1}'
    elif mutation == "malformed":
        data = '{"untrusted": not-json}'
    path.write_text(data)
    with pytest.raises((ValueError, OSError)):
        load_management_settings(config)


@pytest.mark.parametrize("target", ["extension", "token", "config"])
@pytest.mark.parametrize("fault", ["mode", "symlink", "hardlink", "missing"])
def test_unprotected_inputs_refuse(files: tuple[Path, Path], target: str, fault: str) -> None:
    config, token = files
    extension = install(config, token)
    path = {"extension": extension, "token": token, "config": config}[target]
    if fault == "mode":
        path.chmod(0o666)
    elif fault == "hardlink":
        os.link(path, path.with_name(path.name + ".link"))
    else:
        saved = path.with_name(path.name + ".saved")
        path.rename(saved)
        if fault == "symlink":
            path.symlink_to(saved)
    if target == "extension" and fault == "missing":
        assert load_management_settings(config).source_repository is None
    else:
        with pytest.raises((ValueError, OSError)):
            load_management_settings(config)


def test_yaml_change_and_inline_conflict_refuse(files: tuple[Path, Path]) -> None:
    config, token = files
    install(config, token)
    config.write_bytes(config.read_bytes() + b"# changed\n")
    with pytest.raises(ValueError, match="binding"):
        load_management_settings(config)
    install(config, token)
    config.write_bytes(config.read_bytes() + b"source_repository: kitamura-tetsuo/auto-coder\n")
    install(config, token)
    with pytest.raises(ValueError, match="conflict"):
        load_management_settings(config)


def test_changed_read_snapshot_and_parent_permissions_refuse(files: tuple[Path, Path]) -> None:
    config, token = files
    install(config, token)
    with pytest.raises(ValueError, match="binding"):
        load_extension(config, b"old bytes")
    config.parent.chmod(0o777)
    with pytest.raises(PermissionError):
        load_management_settings(config)


def test_preparation_cli_never_reads_or_prints_token(files: tuple[Path, Path]) -> None:
    config, token = files
    command = [sys.executable, "-m", "auto_coder.management.source_evidence_config", "--config", str(config), "--repository", REPOSITORY, "--token-file", str(token)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert json.loads(result.stdout) == prepare(config, REPOSITORY, token)
    assert token.read_text() not in result.stdout + result.stderr
    assert not extension_path(config).exists()
    token.unlink()
    failed = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert failed.returncode == 1 and not failed.stdout
    assert "source evidence configuration unavailable" in failed.stderr


def test_wrong_operator_and_symlink_ancestor_refuse(files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, token = files
    install(config, token)
    alias = config.parent.with_name(config.parent.name + "-alias")
    alias.symlink_to(config.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        load_management_settings(alias / config.name)
    uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    with pytest.raises(PermissionError):
        load_management_settings(config)


def test_token_metadata_validation_does_not_read_contents(files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, token = files
    install(config, token)
    original = os.fdopen

    def no_credential_stream(descriptor: int, *args: object, **kwargs: object):
        assert os.fstat(descriptor).st_ino != token.stat().st_ino
        return original(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", no_credential_stream)
    assert load_management_settings(config).source_repository == REPOSITORY


def test_unreadable_token_refuses_at_read_open_without_disclosure(files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from auto_coder.management.source_evidence_config import main

    config, token = files
    install(config, token)
    original_open = os.open
    attempts = []

    def denied(path, flags, *args, **kwargs):
        if path == token.name:
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            attempts.append(path)
            raise PermissionError("controlled unreadable token")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PermissionError, match="controlled unreadable token"):
        prepare(config, REPOSITORY, token)
    with pytest.raises(PermissionError, match="controlled unreadable token"):
        load_management_settings(config)
    monkeypatch.setattr(sys, "argv", ["source_evidence_config", "--config", str(config), "--repository", REPOSITORY, "--token-file", str(token)])
    with pytest.raises(SystemExit) as exit_status:
        main()
    assert exit_status.value.code == 1 and attempts == [token.name] * 3
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "source evidence configuration unavailable\n"
    assert token.read_text() not in captured.out + captured.err
