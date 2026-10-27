"""Probe retained manager state with both exact installed runtimes before a stop.

Only private SQLite backups are passed to target-version code.  A successful
probe is compatibility evidence, never evidence that a live replacement ran.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .oauth_config import load_management_settings
from .version_staging import ManagementReadyReceipt


class ManagementPreflightError(RuntimeError):
    """A safe category; subprocess output and credential contents are not exposed."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class ManagementPreflightReceipt:
    candidate_receipt_identity: str
    previous_receipt_identity: str
    configuration_sha256: str
    oauth_snapshot_sha256: str
    journal_snapshot_sha256: str
    verified_at: float


@dataclass(frozen=True)
class _TableSnapshot:
    name: str
    columns: tuple[tuple[object, ...], ...]
    rows_sha256: str


@dataclass(frozen=True)
class _DatabaseSnapshot:
    tables: tuple[_TableSnapshot, ...]
    objects: tuple[tuple[str, str, str], ...]
    identity: str


# This assembler imports the installed runtime's production constructors and
# decoders. It intentionally needs no newly added module in the old runtime.
_PROBE = r"""
import hashlib
import json
import secrets
import sqlite3
import sys
import time
from pathlib import Path
import auto_coder
from auto_coder.build_provenance import observe_controller_artifact
from auto_coder.management.action_scopes import PendingAuthorityStatus, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import QueryStatus, ReadStatus
from auto_coder.management.operator_policy import read_operator_policy

fixture, expected_binding, environment, source_sha, distribution_version = sys.argv[1:]
assert Path(sys.prefix).resolve() == Path(environment).resolve()
Path(auto_coder.__file__).resolve().relative_to(Path(environment).resolve())
artifact = observe_controller_artifact()
assert artifact.source_revision.available and artifact.source_revision.value == source_sha
assert artifact.distribution_version.available and artifact.distribution_version.value == distribution_version
settings = load_management_settings(Path(fixture))
observed_binding = {
    'issuer': settings.oauth.canonical_issuer, 'resource': settings.oauth.resource,
    'project': settings.oauth.firebase_project_id, 'enabled_scopes': list(settings.oauth.enabled_scopes),
    'clients': [[client.client_id, list(client.redirect_uris), client.token_endpoint_auth_method, list(client.allowed_scopes)] for client in settings.oauth.clients],
    'host': settings.bind_host, 'port': settings.port, 'local_development': settings.local_development,
    'browser_origins': list(settings.allowed_browser_origins),
}
assert observed_binding == json.loads(expected_binding)
identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / 'allowed_uids.json'))
operator_policy = read_operator_policy(settings.state_root / 'allowed_uids.json', settings.oauth.firebase_project_id)
grants = OAuthGrantService(settings.oauth, identity)
app = create_management_app(settings, grants, worker_clients={})
assert callable(app)
journal = ManagementOperationStore(settings.operation_state_root or settings.state_root)
cursor = 0
while True:
    page = journal.list_operations(cursor=cursor, limit=200)
    assert page.status is QueryStatus.OK
    for record in page.records:
        assert journal.get(record.operation_id).status is ReadStatus.FOUND
        history_cursor = 0
        while True:
            history = journal.history(record.operation_id, cursor=history_cursor, limit=200)
            assert history.status is QueryStatus.OK
            if history.next_cursor is None:
                break
            history_cursor = history.next_cursor
    if page.next_cursor is None:
        break
    cursor = page.next_cursor
with grants.store.connect() as connection:
    assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    for table in ('authority', 'pending', 'codes', 'grants', 'access_tokens', 'refresh_tokens'):
        connection.execute('SELECT * FROM ' + table).fetchall()
# A new disposable credential exercises the installed version's real grant and
# scope/policy decoder. Its secret never leaves this subprocess or enters a log.
assert operator_policy.allowed_uids
probe_id, probe_token = secrets.token_hex(32), secrets.token_urlsafe(32)
probe_hash = hashlib.sha256(probe_token.encode()).hexdigest()
probe_uid = sorted(operator_policy.allowed_uids)[0]
probe_client = settings.oauth.clients[0].client_id
now = time.time()
with grants.store.connect() as connection:
    connection.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)',
        (probe_id, probe_client, settings.oauth.resource, READ_SCOPE, settings.oauth.firebase_project_id, probe_uid, now - 1, now, now + 60))
    connection.execute('INSERT INTO access_tokens VALUES(?,?,?,?)', (probe_hash, probe_id, now + 60, READ_SCOPE))
try:
    authenticated = grants.authenticate_access(probe_token, settings.oauth.resource, (READ_SCOPE,))
    assert authenticated.grant_id == probe_id and authenticated.principal.uid == probe_uid
    reference = grants.accepted_authority(authenticated)
    assert grants.check_pending_authority(reference, (READ_SCOPE,)).status is PendingAuthorityStatus.PERMITTED
finally:
    with grants.store.connect() as connection:
        connection.execute('DELETE FROM access_tokens WHERE hash=?', (probe_hash,))
        connection.execute('DELETE FROM grants WHERE id=?', (probe_id,))
print(json.dumps({'status': 'compatible', 'source_sha': artifact.source_revision.value,
                  'distribution_version': artifact.distribution_version.value,
                  'python_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}))
"""


class ManagementSelfUpdatePreflight:
    """Bounded, no-listener compatibility check over operator-selected state."""

    def __init__(self, config_path: Path, *, timeout_seconds: float = 30) -> None:
        if not config_path.is_absolute() or not 0 < timeout_seconds <= 120:
            raise ValueError("an absolute management configuration and bounded timeout are required")
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds

    def verify(self, candidate: ManagementReadyReceipt, previous: ManagementReadyReceipt) -> ManagementPreflightReceipt:
        """Check a candidate and the exact return runtime without touching live state.

        The caller must first authenticate both READY receipts with the stager.
        This method independently checks installed import/provenance and config
        bindings, and refuses missing state rather than creating an empty store.
        """

        try:
            content = self.config_path.read_bytes()
            configuration_sha256 = hashlib.sha256(content).hexdigest()
            for receipt in (candidate, previous):
                self._validate_runtime(receipt, configuration_sha256)
            if candidate.instance_id != previous.instance_id or candidate.runtime_profile != previous.runtime_profile:
                raise ManagementPreflightError("runtime_identity_mismatch")
            settings = load_management_settings(self.config_path)
            raw = yaml.safe_load(content)
            if not isinstance(raw, dict):
                raise ManagementPreflightError("configuration_incompatible")
            with tempfile.TemporaryDirectory(prefix="auto-coder-management-preflight-") as directory:
                root = Path(directory)
                root.chmod(0o700)
                state = root / "state"
                operations = root / "operations"
                state.mkdir(mode=0o700)
                operations.mkdir(mode=0o700)
                oauth_path = state / "oauth.sqlite3"
                journal_path = operations / "management-operations.sqlite3"
                self._backup(settings.oauth.database_path, oauth_path)
                self._backup((settings.operation_state_root or settings.state_root) / "management-operations.sqlite3", journal_path)
                self._copy_private(settings.state_root / "allowed_uids.json", state / "allowed_uids.json")
                raw["state_root"] = str(state)
                raw["operation_state_root"] = str(operations)
                if settings.operating_policy_path is not None:
                    policy = root / "operating-policy.json"
                    self._copy_private(settings.operating_policy_path, policy)
                    raw["operating_policy_path"] = str(policy)
                # Forwarding is deliberately absent in this disposable app. The
                # stager already validates the original configuration parser;
                # this target probe never receives paths to live operator state.
                for name in ("bootstrap_config_path", "self_update_source_repository", "self_update_python_executable", "self_update_uv_executable", "self_update_uv_version", "source_repository", "source_evidence_token_file"):
                    raw.pop(name, None)
                fixture_config = root / "management.yaml"
                fixture_config.write_text(yaml.safe_dump(raw), encoding="utf-8")
                fixture_config.chmod(0o600)
                configuration_binding = json.dumps(
                    {
                        "issuer": settings.oauth.canonical_issuer,
                        "resource": settings.oauth.resource,
                        "project": settings.oauth.firebase_project_id,
                        "enabled_scopes": list(settings.oauth.enabled_scopes),
                        "clients": [[client.client_id, list(client.redirect_uris), client.token_endpoint_auth_method, list(client.allowed_scopes)] for client in settings.oauth.clients],
                        "host": settings.bind_host,
                        "port": settings.port,
                        "local_development": settings.local_development,
                        "browser_origins": list(settings.allowed_browser_origins),
                    }
                )
                originals = (self._snapshot(oauth_path), self._snapshot(journal_path))
                # Establish that the previous runtime reads the current state,
                # then test the candidate and return to the old executable on
                # precisely the candidate-opened retained-state fixture.
                for receipt, category in ((previous, "previous_incompatible"), (candidate, "candidate_incompatible"), (previous, "previous_return_incompatible")):
                    self._probe(receipt, fixture_config, configuration_binding, root, category)
                    for path, before in zip((oauth_path, journal_path), originals):
                        self._require_preserved(path, before)
                if hashlib.sha256(self.config_path.read_bytes()).hexdigest() != configuration_sha256:
                    raise ManagementPreflightError("configuration_changed")
                return ManagementPreflightReceipt(candidate.receipt_identity, previous.receipt_identity, configuration_sha256, originals[0].identity, originals[1].identity, time.time())
        except ManagementPreflightError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
            raise ManagementPreflightError("state_or_configuration_unavailable") from exc

    @staticmethod
    def _validate_runtime(receipt: ManagementReadyReceipt, configuration_sha256: str) -> None:
        environment = Path(receipt.environment_path)
        interpreter = Path(receipt.interpreter_path)
        if (
            not environment.is_absolute()
            or interpreter != environment / "bin" / "python"
            or not interpreter.is_file()
            or not (environment / "bin" / "auto-coder").is_file()
            or receipt.schema_version != 1
            or receipt.entrypoint != "auto-coder management"
            or not receipt.receipt_identity
            or receipt.configuration_sha256 != configuration_sha256
        ):
            raise ManagementPreflightError("runtime_identity_mismatch")

    @staticmethod
    def _copy_private(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o600)

    def _backup(self, source: Path, destination: Path) -> None:
        # mode=ro forbids creation when an operator-owned database is missing;
        # SQLite backup includes committed WAL data unlike a raw file copy.
        deadline = time.monotonic() + self.timeout_seconds

        def bounded_progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() >= deadline:
                raise ManagementPreflightError("preflight_timeout")

        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5) as original:
            with sqlite3.connect(destination, timeout=5) as copy:
                original.backup(copy, pages=256, progress=bounded_progress, sleep=0.05)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ManagementPreflightError("retained_state_corrupt")
        destination.chmod(0o600)

    def _probe(self, receipt: ManagementReadyReceipt, fixture: Path, configuration_binding: str, root: Path, category: str) -> None:
        environment = {
            "PATH": os.defpath,
            "HOME": str(root),
            "TMPDIR": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AUTO_CODER_DISABLE_AUTO_UPDATE": "1",
            "AUTO_CODER_LOG_FILE": str(root / "preflight.log"),
        }
        try:
            executable = Path(receipt.environment_path) / "bin" / "auto-coder"
            subprocess.run([str(executable), "management", "serve", "--help"], cwd=root, env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=self.timeout_seconds)
            result = subprocess.run(
                [receipt.interpreter_path, "-I", "-B", "-c", _PROBE, str(fixture), configuration_binding, receipt.environment_path, receipt.source_sha, receipt.distribution_version],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            observed = json.loads(result.stdout.strip().splitlines()[-1])
            if observed != {"status": "compatible", "source_sha": receipt.source_sha, "distribution_version": receipt.distribution_version, "python_sha256": receipt.python_executable_sha256}:
                raise ManagementPreflightError("runtime_identity_mismatch")
        except subprocess.TimeoutExpired as exc:
            raise ManagementPreflightError("preflight_timeout") from exc
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError) as exc:
            # Never include captured stdout/stderr: future target code could
            # accidentally print credential material while failing startup.
            raise ManagementPreflightError(category) from exc

    @staticmethod
    def _quoted(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    @classmethod
    def _snapshot(cls, path: Path) -> _DatabaseSnapshot:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ManagementPreflightError("retained_state_corrupt")
            tables = []
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                columns = tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({cls._quoted(name)})"))
                rows = connection.execute(f"SELECT * FROM {cls._quoted(name)}").fetchall()
                digest = cls._rows_digest(rows)
                tables.append(_TableSnapshot(name, columns, digest))
            objects = tuple(connection.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') AND sql IS NOT NULL ORDER BY type,name"))
            identity = hashlib.sha256(repr((tables, objects)).encode()).hexdigest()
            return _DatabaseSnapshot(tuple(tables), objects, identity)

    @staticmethod
    def _rows_digest(rows: list[tuple[object, ...]]) -> str:
        # Hashes leave no row values in the durable receipt. repr preserves
        # SQLite scalar type distinctions, including blobs and NULL values.
        return hashlib.sha256(repr(sorted(repr(tuple(row)) for row in rows)).encode()).hexdigest()

    @classmethod
    def _require_preserved(cls, path: Path, original: _DatabaseSnapshot) -> None:
        after = cls._snapshot(path)
        current = {table.name: table for table in after.tables}
        if not set(original.objects) <= set(after.objects):
            raise ManagementPreflightError("retained_schema_changed")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            for table in original.tables:
                selected = current.get(table.name)
                if selected is None or selected.columns[: len(table.columns)] != table.columns:
                    raise ManagementPreflightError("retained_schema_changed")
                columns = ",".join(cls._quoted(str(column[1])) for column in table.columns)
                rows = connection.execute(f"SELECT {columns} FROM {cls._quoted(table.name)}").fetchall()
                if cls._rows_digest(rows) != table.rows_sha256:
                    raise ManagementPreflightError("retained_state_changed")
