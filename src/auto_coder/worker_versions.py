"""Immutable, side-by-side worker runtime preparation.

The inputs to this module are deliberately split in two: :class:`WorkerVersionConfig`
is trusted controller configuration, while :class:`PrepareIntent` contains only the
full object id and an operation id that a caller is allowed to select.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from loguru import logger

READY_SCHEMA_VERSION = 1
RUNTIME_ID_FILENAME = ".auto-coder-runtime-id"
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class PreparationFailure(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED = "UNSUPPORTED"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    INCOMPATIBLE_DEPENDENCIES = "INCOMPATIBLE_DEPENDENCIES"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    AUTHENTICATION_UNAVAILABLE = "AUTHENTICATION_UNAVAILABLE"
    INSUFFICIENT_SPACE = "INSUFFICIENT_SPACE"
    UNAVAILABLE = "UNAVAILABLE"
    CORRUPT_READY = "CORRUPT_READY"
    CONFLICT = "CONFLICT"
    VERIFICATION_MISMATCH = "VERIFICATION_MISMATCH"


class WorkerVersionError(RuntimeError):
    """A safe, typed preparation failure."""

    def __init__(self, failure: PreparationFailure, message: str):
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True)
class WorkerVersionConfig:
    """Controller-owned configuration; never construct this from MCP arguments."""

    source_repository: Path
    version_root: Path
    python_executable: Path
    uv_executable: Path
    uv_version: str
    runtime_profile: str = "python-3.12"


@dataclass(frozen=True)
class PrepareIntent:
    source_sha: str
    attempt_id: str


@dataclass(frozen=True)
class InstalledProvenance:
    source_revision: str
    distribution_version: str


@dataclass(frozen=True)
class ReadyReceipt:
    schema_version: int
    source_sha: str
    resolved_commit: str
    environment_path: str
    interpreter_path: str
    distribution_version: str
    lock_sha256: str
    attempt_id: str
    runtime_profile: str
    installed_runtime_id: str
    content_identity: str
    installed_provenance: InstalledProvenance
    installed_at: str


@dataclass(frozen=True)
class PreparedEnvironment:
    receipt: ReadyReceipt
    reused: bool = False


class WorkerVersionPreparer:
    """Prepare and verify immutable Auto-Coder environments in one state root."""

    def __init__(self, config: WorkerVersionConfig):
        self.config = config
        self.root = config.version_root.expanduser().resolve()
        self.repository = config.source_repository.expanduser().resolve()

    def prepare(self, intent: PrepareIntent) -> PreparedEnvironment:
        self._validate_intent(intent)
        self._validate_configuration()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self.root / ".prepare.lock"
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                return self._prepare_locked(intent)
        except WorkerVersionError:
            raise
        except OSError as exc:
            raise self._filesystem_error(exc) from exc

    def read_ready(self, receipt_path: Path) -> ReadyReceipt:
        """Read a READY receipt and independently verify all immutable bindings."""

        try:
            target = receipt_path.resolve(strict=True)
            target.relative_to(self.root)
            raw = json.loads(target.read_text(encoding="utf-8"))
            receipt = self._decode_receipt(raw)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise WorkerVersionError(PreparationFailure.CORRUPT_READY, "READY receipt is unreadable or malformed") from exc
        try:
            self._validate_receipt(receipt, target.parent)
        except WorkerVersionError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkerVersionError(PreparationFailure.CORRUPT_READY, "READY receipt contains invalid values") from exc
        return receipt

    @staticmethod
    def _decode_receipt(raw: object) -> ReadyReceipt:
        """Decode the bounded receipt schema before any field reaches path/regex APIs."""

        if not isinstance(raw, dict):
            raise TypeError("receipt must be an object")
        expected = {field.name for field in ReadyReceipt.__dataclass_fields__.values()}
        if set(raw) != expected or not isinstance(raw.get("installed_provenance"), dict):
            raise TypeError("receipt fields do not match the schema")
        provenance_raw = raw["installed_provenance"]
        if set(provenance_raw) != {"source_revision", "distribution_version"}:
            raise TypeError("installed provenance fields do not match the schema")
        string_fields = expected - {"schema_version", "installed_provenance"}
        if type(raw["schema_version"]) is not int or any(not isinstance(raw[name], str) or not raw[name] for name in string_fields):
            raise TypeError("receipt scalar fields have invalid types")
        if any(not isinstance(provenance_raw[name], str) or not provenance_raw[name] for name in provenance_raw):
            raise TypeError("installed provenance values have invalid types")
        values = dict(raw)
        values["installed_provenance"] = InstalledProvenance(**provenance_raw)
        return ReadyReceipt(**values)

    def _prepare_locked(self, intent: PrepareIntent) -> PreparedEnvironment:
        resolved = self._run(["git", "-C", str(self.repository), "rev-parse", "--verify", f"{intent.source_sha}^{{commit}}"], output=True).strip()
        if resolved != intent.source_sha:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "configured repository does not contain the exact requested commit")
        lock_result = self._run_bytes(["git", "-C", str(self.repository), "show", f"{intent.source_sha}:uv.lock"])
        desired_lock = hashlib.sha256(lock_result).hexdigest()
        key = hashlib.sha256(f"{intent.source_sha}\0{desired_lock}\0{self.config.runtime_profile}".encode()).hexdigest()
        revisions_root = self._confined_directory(self.root / "revisions")
        revisions = self._confined_directory(revisions_root / key)
        for ready in sorted(revisions.glob("*/READY.json")):
            try:
                receipt = self.read_ready(ready)
            except WorkerVersionError as exc:
                raise WorkerVersionError(PreparationFailure.CONFLICT, "retained environment has an invalid READY receipt") from exc
            if receipt.source_sha == intent.source_sha and receipt.lock_sha256 == desired_lock and receipt.runtime_profile == self.config.runtime_profile:
                return PreparedEnvironment(receipt, reused=True)

        attempt = revisions / intent.attempt_id
        if attempt.exists():
            raise WorkerVersionError(PreparationFailure.CONFLICT, "preparation attempt already exists without verified READY state")
        checkout = attempt / "source"
        environment = attempt / "venv"
        self._confined_directory(attempt, mode=0o700)
        try:
            self._checkout(checkout, intent.source_sha)
            lock_bytes = (checkout / "uv.lock").read_bytes()
            lock_hash = hashlib.sha256(lock_bytes).hexdigest()
            if lock_hash != desired_lock:
                raise WorkerVersionError(PreparationFailure.CONFLICT, "selected revision lock differs from the requested locked input")
            self._install(checkout, environment)
            if (checkout / "uv.lock").read_bytes() != lock_bytes:
                raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installer changed uv.lock")
            observed = self._observe(environment)
            if observed.source_revision != intent.source_sha:
                raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed source revision does not match requested commit")
            # Keep the venv launcher path. Resolving it would record the base Python
            # behind uv's symlink and make the receipt unusable by an activator.
            interpreter = environment.absolute() / "bin" / "python"
            if not interpreter.exists():
                raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed environment has no Python launcher")
            runtime_id = uuid.uuid4().hex
            self._write_runtime_id(environment, runtime_id)
            bindings = {
                "attempt_id": intent.attempt_id,
                "runtime_profile": self.config.runtime_profile,
                "environment_path": str(environment.resolve()),
                "interpreter_path": str(interpreter),
                "source_sha": intent.source_sha,
                "distribution_version": observed.distribution_version,
                "lock_sha256": lock_hash,
                "installed_runtime_id": runtime_id,
            }
            content_identity = hashlib.sha256(json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            receipt = ReadyReceipt(
                schema_version=READY_SCHEMA_VERSION,
                source_sha=intent.source_sha,
                resolved_commit=intent.source_sha,
                environment_path=bindings["environment_path"],
                interpreter_path=bindings["interpreter_path"],
                distribution_version=observed.distribution_version,
                lock_sha256=lock_hash,
                attempt_id=intent.attempt_id,
                runtime_profile=self.config.runtime_profile,
                installed_runtime_id=runtime_id,
                content_identity=content_identity,
                installed_provenance=observed,
                installed_at=datetime.now(timezone.utc).isoformat(),
            )
            self._publish(attempt / "READY.json", receipt)
            logger.info("Prepared immutable worker revision {}", intent.source_sha[:12])
            return PreparedEnvironment(self.read_ready(attempt / "READY.json"))
        except WorkerVersionError:
            raise
        except OSError as exc:
            raise self._filesystem_error(exc) from exc
        except subprocess.SubprocessError as exc:
            raise WorkerVersionError(PreparationFailure.UNAVAILABLE, "worker revision preparation was unavailable") from exc

    def _checkout(self, destination: Path, source_sha: str) -> None:
        self._run(["git", "clone", "--no-checkout", "--", str(self.repository), str(destination)])
        self._run(["git", "-C", str(destination), "fetch", "--no-tags", "origin", source_sha])
        resolved = self._run(["git", "-C", str(destination), "rev-parse", "--verify", f"{source_sha}^{{commit}}"], output=True).strip()
        if resolved != source_sha:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "Git did not resolve the exact requested commit")
        self._run(["git", "-C", str(destination), "checkout", "--detach", source_sha])
        status = self._run(["git", "-C", str(destination), "status", "--porcelain=v1", "--untracked-files=all"], output=True)
        if status:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "selected checkout is dirty or inconsistent")

    def _install(self, checkout: Path, environment: Path) -> None:
        self._run([str(self.config.uv_executable), "venv", "--python", str(self.config.python_executable), "--no-project", str(environment)])
        env = self._safe_environment()
        env["VIRTUAL_ENV"] = str(environment)
        self._run([str(self.config.uv_executable), "sync", "--frozen", "--no-editable", "--project", str(checkout), "--active"], env=env)
        # Embed through the installed artifact, not through the manager checkout.
        env["AUTO_CODER_SOURCE_REVISION"] = self._run(["git", "-C", str(checkout), "rev-parse", "HEAD"], output=True).strip()
        self._run([str(environment / "bin" / "python"), "-m", "auto_coder.build_provenance", "embed"], env=env)
        self._verify_entrypoint(environment)

    def _verify_entrypoint(self, environment: Path) -> None:
        try:
            self._run([str(environment / "bin" / "auto-coder"), "--help"], env=self._safe_environment())
        except WorkerVersionError as exc:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed worker entry point is unavailable or failed") from exc

    def _observe(self, environment: Path) -> InstalledProvenance:
        output = self._run([str(environment / "bin" / "python"), "-m", "auto_coder.build_provenance"], output=True, env=self._safe_environment())
        try:
            value = json.loads(output)
            source = value["source_revision"]
            version = value["distribution_version"]
            if not source["available"] or not version["available"]:
                raise ValueError("unavailable installed identity")
            return InstalledProvenance(source_revision=source["value"], distribution_version=version["value"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed provenance response is invalid") from exc

    def _validate_receipt(self, receipt: ReadyReceipt, attempt: Path) -> None:
        if (
            receipt.schema_version != READY_SCHEMA_VERSION
            or not _FULL_SHA.fullmatch(receipt.source_sha)
            or receipt.resolved_commit != receipt.source_sha
            or not _SHA256.fullmatch(receipt.lock_sha256)
            or not _SHA256.fullmatch(receipt.content_identity)
            or not _SAFE_NAME.fullmatch(receipt.attempt_id)
            or not _SAFE_NAME.fullmatch(receipt.runtime_profile)
            or receipt.installed_provenance.source_revision != receipt.source_sha
        ):
            raise WorkerVersionError(PreparationFailure.CORRUPT_READY, "READY receipt identity is invalid")
        environment = Path(receipt.environment_path)
        interpreter = Path(receipt.interpreter_path)
        expected_environment = attempt.absolute() / "venv"
        expected_interpreter = expected_environment / "bin" / "python"
        if not environment.is_absolute() or not interpreter.is_absolute() or environment != expected_environment or interpreter != expected_interpreter:
            raise WorkerVersionError(PreparationFailure.CONFLICT, "READY paths do not identify their exact attempt environment")
        try:
            environment.resolve(strict=True).relative_to(attempt.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise WorkerVersionError(PreparationFailure.CONFLICT, "READY environment is outside its attempt") from exc
        if not interpreter.exists() or self._interpreter_prefix(interpreter) != environment:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "READY interpreter does not execute in its recorded environment")
        if self._read_runtime_id(environment) != receipt.installed_runtime_id:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "READY runtime identity does not match its installed environment")
        checkout_lock = attempt / "source" / "uv.lock"
        try:
            observed_lock = hashlib.sha256(checkout_lock.read_bytes()).hexdigest()
        except OSError as exc:
            raise WorkerVersionError(PreparationFailure.UNAVAILABLE, "READY locked input is unavailable") from exc
        observed = self._observe(environment)
        bindings = {
            "attempt_id": receipt.attempt_id,
            "runtime_profile": receipt.runtime_profile,
            "environment_path": receipt.environment_path,
            "interpreter_path": receipt.interpreter_path,
            "source_sha": receipt.source_sha,
            "distribution_version": receipt.distribution_version,
            "lock_sha256": receipt.lock_sha256,
            "installed_runtime_id": receipt.installed_runtime_id,
        }
        identity = hashlib.sha256(json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if observed_lock != receipt.lock_sha256 or observed != receipt.installed_provenance or observed.source_revision != receipt.source_sha or observed.distribution_version != receipt.distribution_version or identity != receipt.content_identity:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "READY bindings no longer match the installed runtime")

    def _interpreter_prefix(self, interpreter: Path) -> Path:
        output = self._run([str(interpreter), "-c", "import sys; print(sys.prefix)"], output=True, env=self._safe_environment()).strip()
        try:
            return Path(output).resolve(strict=True)
        except OSError as exc:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed interpreter returned an invalid environment prefix") from exc

    def _validate_configuration(self) -> None:
        if not self.repository.is_dir() or self.root == self.repository or self.repository in self.root.parents:
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "source repository or version root is invalid")
        if not _SAFE_NAME.fullmatch(self.config.runtime_profile):
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "runtime profile is invalid")
        python_version = self._run([str(self.config.python_executable), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], output=True).strip()
        if python_version != "3.12":
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "configured interpreter is not Python 3.12")
        uv_version = self._run([str(self.config.uv_executable), "--version"], output=True).strip()
        if uv_version != f"uv {self.config.uv_version}":
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "bootstrap uv version does not match the configured pin")
        self._run(["git", "--version"], output=True)

    @staticmethod
    def _validate_intent(intent: PrepareIntent) -> None:
        if not _FULL_SHA.fullmatch(intent.source_sha) or not _SAFE_NAME.fullmatch(intent.attempt_id):
            raise WorkerVersionError(PreparationFailure.INVALID_REQUEST, "a full lowercase source SHA and safe attempt id are required")

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}

    @staticmethod
    def _run(arguments: list[str], *, output: bool = False, env: dict[str, str] | None = None) -> str:
        try:
            result = subprocess.run(arguments, check=True, capture_output=True, text=True, env=env)
        except FileNotFoundError as exc:
            raise WorkerVersionError(PreparationFailure.MISSING_CAPABILITY, "required bootstrap executable is unavailable") from exc
        except subprocess.CalledProcessError as exc:
            # Do not include subprocess output: it may contain repository credentials.
            raise WorkerVersionError(WorkerVersionPreparer._classify_process_failure(arguments, exc.stderr), "Git or uv operation failed") from exc
        return result.stdout if output else ""

    @staticmethod
    def _run_bytes(arguments: list[str]) -> bytes:
        try:
            return subprocess.run(arguments, check=True, capture_output=True).stdout
        except FileNotFoundError as exc:
            raise WorkerVersionError(PreparationFailure.MISSING_CAPABILITY, "required bootstrap executable is unavailable") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            raise WorkerVersionError(WorkerVersionPreparer._classify_process_failure(arguments, stderr), "requested Git object or locked input is unavailable") from exc

    @staticmethod
    def _classify_process_failure(arguments: list[str], stderr: str | None) -> PreparationFailure:
        """Classify diagnostics without retaining or logging their sensitive text."""

        diagnostic = (stderr or "").lower()
        if "no space left" in diagnostic or "disk quota" in diagnostic:
            return PreparationFailure.INSUFFICIENT_SPACE
        if any(term in diagnostic for term in ("authentication failed", "permission denied", "could not read username", "http 401", "http 403")):
            return PreparationFailure.AUTHENTICATION_UNAVAILABLE
        if any(term in diagnostic for term in ("could not resolve host", "network is unreachable", "connection timed out", "connection refused")):
            return PreparationFailure.NETWORK_UNAVAILABLE
        executable = Path(arguments[0]).name
        if executable == "uv" or "uv" in executable:
            return PreparationFailure.INCOMPATIBLE_DEPENDENCIES
        return PreparationFailure.UNAVAILABLE

    @staticmethod
    def _filesystem_error(exc: OSError) -> WorkerVersionError:
        """Normalize preparation filesystem failures without exposing paths."""

        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            return WorkerVersionError(PreparationFailure.INSUFFICIENT_SPACE, "worker revision storage has insufficient space")
        return WorkerVersionError(PreparationFailure.UNAVAILABLE, "worker revision storage is unavailable")

    def _confined_directory(self, path: Path, *, mode: int = 0o700) -> Path:
        """Create one state directory without following retained symlinks."""

        try:
            path.relative_to(self.root)
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_dir():
                    raise WorkerVersionError(PreparationFailure.CONFLICT, "retained version state has an invalid directory structure")
            else:
                path.mkdir(mode=mode)
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
            return resolved
        except WorkerVersionError:
            raise
        except ValueError as exc:
            raise WorkerVersionError(PreparationFailure.CONFLICT, "retained version state escapes the configured root") from exc
        except OSError as exc:
            raise self._filesystem_error(exc) from exc

    @staticmethod
    def _write_runtime_id(environment: Path, runtime_id: str) -> None:
        marker = environment / RUNTIME_ID_FILENAME
        try:
            with marker.open("x", encoding="ascii") as stream:
                stream.write(runtime_id + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            marker.chmod(0o444)
        except OSError as exc:
            raise WorkerVersionPreparer._filesystem_error(exc) from exc

    @staticmethod
    def _read_runtime_id(environment: Path) -> str:
        try:
            runtime_id = (environment / RUNTIME_ID_FILENAME).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed runtime identity is unavailable") from exc
        if not re.fullmatch(r"[0-9a-f]{32}", runtime_id):
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed runtime identity is invalid")
        return runtime_id

    @staticmethod
    def _publish(path: Path, receipt: ReadyReceipt) -> None:
        payload = asdict(receipt)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".READY-", delete=False) as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
