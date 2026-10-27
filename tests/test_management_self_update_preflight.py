"""Real installed-runtime probes over disposable copies of production stores."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import sysconfig
import venv
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.oauth_store import OAuthStore
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, ManagedTarget, OperationIntent
from auto_coder.management.self_update_preflight import ManagementPreflightError, ManagementSelfUpdatePreflight
from auto_coder.management.version_staging import ManagementReadyReceipt


def _installed_runtime(root: Path, source_sha: str, configuration: Path) -> tuple[ManagementReadyReceipt, Path]:
    """Install an independent production-package copy with real runtime imports.

    Dependencies are shared with the test interpreter; auto_coder itself and its
    metadata live exclusively inside this private venv. No subprocess response,
    application constructor, database decoder, or compatibility result is faked.
    """

    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    site = root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "test_dependencies.pth").write_text(sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8")
    package = site / "auto_coder"
    shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "auto_coder", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    version = "1.0.0"
    metadata = site / f"auto_coder-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: auto-coder\nVersion: {version}\n", encoding="utf-8")
    (package / "build_provenance.json").write_text(json.dumps({"schema_version": 1, "source_revision": source_sha}), encoding="utf-8")
    executable = root / "bin" / "auto-coder"
    executable.write_text(f"#!{root / 'bin' / 'python'}\nfrom auto_coder.cli import main\nmain()\n", encoding="utf-8")
    executable.chmod(0o700)
    identity = hashlib.sha256(source_sha.encode()).hexdigest()
    receipt = ManagementReadyReceipt(
        schema_version=1,
        management_runtime_id=identity,
        instance_id="manager",
        source_sha=source_sha,
        source_tree="c" * 40,
        tested_source_ref=f"refs/tags/auto-coder-tested-{source_sha}",
        tested_source_object="d" * 40,
        test_run_id=17,
        test_run_attempt=1,
        lock_sha256="e" * 64,
        runtime_profile="management-python-3.12",
        python_version="3.12",
        python_full_version=sys.version,
        python_abi=sysconfig.get_config_var("SOABI") or "",
        python_implementation=sys.implementation.name,
        python_executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        installer_identity="test-production-package-copy",
        environment_path=str(root),
        interpreter_path=str(root / "bin" / "python"),
        distribution_version=version,
        entrypoint="auto-coder management",
        worker_receipt_identity="f" * 64,
        installed_content_sha256="9" * 64,
        configuration_sha256=hashlib.sha256(configuration.read_bytes()).hexdigest(),
        receipt_identity=identity,
    )
    return receipt, package


@pytest.fixture
def retained_state(tmp_path: Path):
    state = tmp_path / "state"
    operations = tmp_path / "journal"
    state.mkdir()
    config = tmp_path / "management.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "state_root": str(state),
                "operation_state_root": str(operations),
                "public_origin": "http://127.0.0.1:18081",
                "bind_host": "127.0.0.1",
                "port": 18081,
                "firebase_project_id": "project",
                "local_development": True,
                "clients": [{"client_id": "client", "redirect_uris": ["http://127.0.0.1:19191/callback"]}],
            }
        ),
        encoding="utf-8",
    )
    (state / "allowed_uids.json").write_text(json.dumps({"project_id": "project", "allowed_uids": ["operator"]}), encoding="utf-8")
    settings = load_management_settings(config)
    oauth = OAuthStore(settings.oauth)
    # Keep this connection open: the committed row must be read from the WAL by
    # SQLite's online backup, rather than lost by a raw main-file copy.
    connection = oauth.connect()
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("original-grant", "client", settings.oauth.resource, "auto-coder.read", "project", "operator", 1, 1, 9999999999, None, None))
    connection.execute("INSERT INTO access_tokens VALUES(?,?,?,?)", ("retained-token-hash", "original-grant", 9999999999, "auto-coder.read"))
    journal = ManagementOperationStore(operations)
    operation = journal.admit(
        OperationIntent(ManagedTarget("manager", "management", "owner/repo"), "update_management_service", (ActionParameter("source_sha", "b" * 40),), "original-key"),
        AcceptedAuthority(settings.oauth.canonical_issuer, settings.oauth.resource, "project", "operator", "client", "original-grant", ("auto-coder.read", "auto-coder.deploy"), 1, 2),
    ).record
    assert operation is not None
    old, old_package = _installed_runtime(tmp_path / "old", "a" * 40, config)
    candidate, candidate_package = _installed_runtime(tmp_path / "candidate", "b" * 40, config)
    yield config, settings, journal, operation, old, old_package, candidate, candidate_package
    connection.close()


def _live_snapshot(settings, journal):
    return (
        ManagementSelfUpdatePreflight._snapshot(settings.oauth.database_path),
        ManagementSelfUpdatePreflight._snapshot(journal._path),
        (settings.state_root / "allowed_uids.json").read_bytes(),
    )


def _change_constructor(package: Path, statements: str) -> None:
    path = package / "management" / "oauth_store.py"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n_original_constructor = OAuthStore.__init__\ndef _candidate_constructor(self, settings):\n    _original_constructor(self, settings)\n")
        stream.write("    with self.connect() as connection:\n")
        for statement in statements.splitlines():
            stream.write("        " + statement + "\n")
        stream.write("OAuthStore.__init__ = _candidate_constructor\n")


def test_real_candidate_and_previous_read_same_wal_state_without_live_changes(retained_state, _use_real_commands) -> None:
    config, settings, journal, operation, previous, _, candidate, _ = retained_state
    before = _live_snapshot(settings, journal)

    result = ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert result.candidate_receipt_identity == candidate.receipt_identity
    assert result.previous_receipt_identity == previous.receipt_identity
    assert result.configuration_sha256 == hashlib.sha256(config.read_bytes()).hexdigest()
    assert result.oauth_snapshot_sha256 == before[0].identity
    assert result.journal_snapshot_sha256 == before[1].identity
    assert result.verified_at > 0
    assert _live_snapshot(settings, journal) == before
    assert journal.get(operation.operation_id).record == operation


def test_worker_source_authority_is_removed_from_each_private_installed_probe(retained_state, _use_real_commands) -> None:
    config, _, journal, operation, previous, old_package, candidate, candidate_package = retained_state
    token_file = config.parent / "source-evidence-token"
    token_file.write_text("disposable-source-read-credential", encoding="utf-8")
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw.update(source_repository="kitamura-tetsuo/auto-coder", source_evidence_token_file=str(token_file))
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_management_settings(config)
    assert settings.source_repository == "kitamura-tetsuo/auto-coder"
    assert settings.source_evidence_token_file == token_file
    configuration = config.read_bytes()
    credential = token_file.read_bytes()
    configuration_sha256 = hashlib.sha256(configuration).hexdigest()
    previous = replace(previous, configuration_sha256=configuration_sha256)
    candidate = replace(candidate, configuration_sha256=configuration_sha256)
    before = _live_snapshot(settings, journal)

    # Execute the real installed app constructor in all three private probes.
    # A retained live source credential path must fail before app construction;
    # no mocked constructor or synthetic success response can satisfy the test.
    for package in (old_package, candidate_package):
        with (package / "management" / "app.py").open("a", encoding="utf-8") as stream:
            stream.write(
                "\n_original_create_management_app = create_management_app\n"
                "def create_management_app(settings, *args, **kwargs):\n"
                "    assert settings.source_repository is None\n"
                "    assert settings.source_evidence_token_file is None\n"
                "    return _original_create_management_app(settings, *args, **kwargs)\n"
            )

    receipt = ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert receipt.candidate_receipt_identity == candidate.receipt_identity
    assert receipt.previous_receipt_identity == previous.receipt_identity
    assert receipt.configuration_sha256 == configuration_sha256
    assert receipt.oauth_snapshot_sha256 == before[0].identity
    assert receipt.journal_snapshot_sha256 == before[1].identity
    assert _live_snapshot(settings, journal) == before
    assert journal.get(operation.operation_id).record == operation
    assert config.read_bytes() == configuration
    assert token_file.read_bytes() == credential


@pytest.mark.parametrize("mutation", ["DELETE FROM access_tokens", "DELETE FROM grants", "ALTER TABLE access_tokens RENAME COLUMN scope TO broken_scope"])
def test_candidate_destructive_store_initialization_is_rejected_without_touching_live_state(retained_state, mutation: str, _use_real_commands) -> None:
    config, settings, journal, _, previous, _, candidate, package = retained_state
    before = _live_snapshot(settings, journal)
    if mutation == "DELETE FROM grants":
        mutation = "DELETE FROM access_tokens; DELETE FROM grants"
    _change_constructor(package, f"connection.executescript({mutation!r})")

    with pytest.raises(ManagementPreflightError) as caught:
        ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert caught.value.category in {"candidate_incompatible", "retained_state_changed", "retained_schema_changed"}
    assert _live_snapshot(settings, journal) == before


def test_candidate_additive_schema_requires_successful_previous_runtime_return(retained_state, _use_real_commands) -> None:
    config, settings, journal, _, previous, old_package, candidate, package = retained_state
    before = _live_snapshot(settings, journal)
    _change_constructor(package, "connection.execute('CREATE TABLE IF NOT EXISTS candidate_layout (value TEXT)')")
    _change_constructor(old_package, "assert connection.execute(\"SELECT name FROM sqlite_master WHERE name='candidate_layout'\").fetchone() is None")

    with pytest.raises(ManagementPreflightError) as caught:
        ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert caught.value.category == "previous_return_incompatible"
    assert _live_snapshot(settings, journal) == before


def test_compatible_additive_schema_does_not_require_live_migration(retained_state, _use_real_commands) -> None:
    config, settings, journal, _, previous, _, candidate, package = retained_state
    before = _live_snapshot(settings, journal)
    _change_constructor(package, "connection.execute('CREATE TABLE IF NOT EXISTS candidate_layout (value TEXT)')")

    assert ManagementSelfUpdatePreflight(config).verify(candidate, previous).candidate_receipt_identity == candidate.receipt_identity

    assert _live_snapshot(settings, journal) == before


@pytest.mark.parametrize("binding", ["source", "python", "configuration", "environment"])
def test_actual_installed_identity_must_match_each_verified_receipt(retained_state, binding: str, _use_real_commands) -> None:
    config, settings, journal, _, previous, _, candidate, _ = retained_state
    before = _live_snapshot(settings, journal)
    if binding == "source":
        candidate = replace(candidate, source_sha="c" * 40)
    elif binding == "python":
        candidate = replace(candidate, python_executable_sha256="0" * 64)
    elif binding == "configuration":
        candidate = replace(candidate, configuration_sha256="0" * 64)
    else:
        candidate = replace(candidate, environment_path=previous.environment_path)

    with pytest.raises(ManagementPreflightError) as caught:
        ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert caught.value.category in {"candidate_incompatible", "runtime_identity_mismatch"}
    assert _live_snapshot(settings, journal) == before


def test_missing_retained_journal_is_never_reinitialized(retained_state, _use_real_commands) -> None:
    config, settings, journal, _, previous, _, candidate, _ = retained_state
    journal._path.unlink()

    with pytest.raises(ManagementPreflightError) as caught:
        ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert caught.value.category == "state_or_configuration_unavailable"
    assert not journal._path.exists()
    assert settings.oauth.database_path.exists()


def test_target_failure_output_is_not_exposed_as_preflight_reason(retained_state, capsys, _use_real_commands) -> None:
    config, settings, journal, _, previous, _, candidate, package = retained_state
    before = _live_snapshot(settings, journal)
    _change_constructor(package, "print('sensitive-target-output')\nraise RuntimeError('sensitive-target-error')")

    with pytest.raises(ManagementPreflightError) as caught:
        ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert str(caught.value) == "candidate_incompatible"
    output = capsys.readouterr()
    assert "sensitive-target" not in output.out + output.err
    assert _live_snapshot(settings, journal) == before


def test_incompatible_operation_schema_refuses_before_candidate_activation(retained_state, _use_real_commands) -> None:
    config, settings, journal, _, previous, _, candidate, _ = retained_state
    with sqlite3.connect(journal._path) as connection:
        connection.execute("UPDATE metadata SET value='unreadable' WHERE key='schema_version'")
    before = _live_snapshot(settings, journal)

    with pytest.raises(ManagementPreflightError) as caught:
        ManagementSelfUpdatePreflight(config).verify(candidate, previous)

    assert caught.value.category == "previous_incompatible"
    assert _live_snapshot(settings, journal) == before
