"""Observation-only, launch-bound authenticated management readback."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .bootstrap_store import ChildPhase, ChildRecord
from .bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor, RuntimeSpec
from .oauth_config import ManagementSettings
from .oauth_grants import AuthenticatedGrant
from .self_update_handoff import HandoffStore, observe_supervisor, runtime_for_receipt


@dataclass(frozen=True)
class ReadinessProof:
    observation_id: str
    instance_id: str
    endpoint: str
    child_identity: str
    launch_nonce: str
    runtime_receipt: str
    source_receipt: str
    application_path: str
    application_sha256: str
    management_config_path: str
    management_config_sha256: str
    project_id: str
    uid: str
    client_id: str
    grant_id: str
    resource: str
    observed_at: float


def _runtime(config: BootstrapConfig, child: ChildRecord) -> RuntimeSpec | None:
    runtime = next((item for item in config.runtimes if item.reference == child.selected_runtime), None)
    if runtime is None and config.management_version_root is not None:
        receipt = HandoffStore(config.state_root, config.instance_id).runtime(child.selected_runtime, readonly=True)
        if receipt is not None:
            runtime = runtime_for_receipt(config, receipt)
    return runtime


def _live(config: BootstrapConfig) -> tuple[ChildRecord, RuntimeSpec] | None:
    observed = observe_supervisor(config)
    if observed is None:
        return None
    incarnation, child = observed
    if child.phase is not ChildPhase.RUNNING or child.supervisor_incarnation != incarnation or not child.drain_nonce:
        return None
    runtime = _runtime(config, child)
    if runtime is None or (child.runtime_receipt, child.source_receipt) != (runtime.receipt, runtime.source_receipt):
        return None
    if not runtime.verify(initial=runtime.reference == config.initial_runtime):
        return None
    environment = dict(item.split(b"=", 1) for item in Path(f"/proc/{child.pid}/environ").read_bytes().split(b"\0") if b"=" in item)
    if any(environment.get(os.fsencode(key)) != os.fsencode(value) for key, value in runtime.environment):
        return None
    if environment.get(b"AUTO_CODER_CHILD_GENERATION") != str(child.child_generation).encode() or environment.get(b"AUTO_CODER_DRAIN_NONCE") != child.drain_nonce.encode():
        return None
    command = Path(f"/proc/{child.pid}/cmdline").read_bytes().split(b"\0")
    if command and command[-1] == b"":
        command.pop()
    arguments = [os.fsencode(arg) for arg in runtime.arguments]
    executable = os.fsencode(str(runtime.executable))
    with runtime.executable.open("rb") as stream:
        first_line = stream.readline(4096)
    if first_line.startswith(b"#!"):
        shebang = first_line[2:].strip().split(maxsplit=1)
        if not shebang or not shebang[0].startswith(b"/"):
            return None
        if shebang[0].endswith(b"/env"):
            if len(shebang) != 2:
                return None
            option = os.fsdecode(shebang[1])
            words = shlex.split(option[3:]) if option.startswith("-S ") else [option]
            if not words or words[0].startswith("-") or "=" in words[0]:
                return None
            search_path = os.fsdecode(environment.get(b"PATH", os.fsencode(os.defpath)))
            resolved = shutil.which(words[0], path=search_path)
            if resolved is None or not os.path.samefile(f"/proc/{child.pid}/exe", resolved):
                return None
            prefix = [os.fsencode(word) for word in words]
        else:
            if not os.path.samefile(f"/proc/{child.pid}/exe", os.fsdecode(shebang[0])):
                return None
            prefix = shebang
        if command != [*prefix, executable, *arguments]:
            return None
    elif not os.path.samefile(f"/proc/{child.pid}/exe", runtime.executable) or command != [executable, *arguments]:
        return None
    return child, runtime


def _connect(config: BootstrapConfig, *, readonly: bool = False) -> sqlite3.Connection:
    path = config.state_root / "management-readiness.sqlite3"
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    connection = sqlite3.connect(path, timeout=2)
    connection.execute("CREATE TABLE IF NOT EXISTS proof (instance_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    return connection


def publish_authenticated_readback(settings: ManagementSettings, grant: AuthenticatedGrant, *, startup_reconciled: bool, observation_id: str) -> None:
    """Persist non-secret evidence only from this authenticated, running child."""
    if not startup_reconciled or settings.bootstrap_config_path is None or settings.config_path is None or len(observation_id) != 32 or any(c not in "0123456789abcdef" for c in observation_id):
        return
    try:
        config = BootstrapConfig.read(settings.bootstrap_config_path)
        endpoint = urlsplit(config.listener_url)
        if (
            config.oauth_root != settings.state_root
            or config.journal_root != settings.operation_state_root
            or endpoint.hostname != settings.bind_host
            or endpoint.port != settings.port
            or endpoint.path != "/mcp"
            or endpoint.query
            or endpoint.fragment
            or (config.management_config is not None and config.management_config != settings.config_path)
        ):
            return
        live = _live(config)
        if live is None:
            return
        child, runtime = live
        if child.pid != os.getpid() or os.environ.get("AUTO_CODER_CHILD_GENERATION") != str(child.child_generation) or os.environ.get("AUTO_CODER_DRAIN_NONCE") != child.drain_nonce:
            return
        import auto_coder

        if auto_coder.__file__ is None:
            return
        application = Path(auto_coder.__file__).resolve(strict=True)
        if runtime.execution_kind == "management-installed" and (runtime.source_root is None or not application.is_relative_to(runtime.source_root.resolve())):
            return
        if runtime.reference != config.initial_runtime and runtime.execution_kind != "management-installed" and (runtime.source_root is None or not application.is_relative_to(runtime.source_root.resolve())):
            return
        proof = ReadinessProof(
            observation_id,
            config.instance_id,
            config.listener_url,
            ManagementBootstrapSupervisor._identity(child),
            child.drain_nonce,
            child.runtime_receipt,
            child.source_receipt,
            str(application),
            hashlib.sha256(application.read_bytes()).hexdigest(),
            str(settings.config_path),
            hashlib.sha256(settings.config_path.read_bytes()).hexdigest(),
            grant.principal.project_id,
            grant.principal.uid,
            grant.client_id,
            grant.grant_id,
            grant.resource,
            time.time(),
        )
        if _live(config) != live:
            return
        with _connect(config) as connection:
            connection.execute("INSERT OR REPLACE INTO proof VALUES (?, ?)", (config.instance_id, json.dumps(asdict(proof), sort_keys=True)))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        # The protected identity read remains usable; no readiness is asserted.
        return


def read_authenticated_readback(config: BootstrapConfig, *, observation_id: str, observation_started_at: float, observation_ends_at: float) -> ReadinessProof | None:
    """Read bounded evidence; None covers all unavailable and mismatched cases."""
    try:
        now = time.time()
        if not all(math.isfinite(value) for value in (observation_started_at, observation_ends_at, now)) or not observation_started_at <= now <= observation_ends_at or observation_ends_at - observation_started_at > config.verification_timeout:
            return None
        live = _live(config)
        if live is None:
            return None
        with _connect(config, readonly=True) as connection:
            row = connection.execute("SELECT payload FROM proof WHERE instance_id = ?", (config.instance_id,)).fetchone()
        if row is None:
            return None
        proof = ReadinessProof(**json.loads(row[0]))
        child, _ = live
        if (proof.observation_id, proof.instance_id, proof.endpoint, proof.child_identity, proof.launch_nonce, proof.runtime_receipt, proof.source_receipt) != (
            observation_id,
            config.instance_id,
            config.listener_url,
            ManagementBootstrapSupervisor._identity(child),
            child.drain_nonce,
            child.runtime_receipt,
            child.source_receipt,
        ):
            return None
        if not isinstance(proof.observed_at, (int, float)) or isinstance(proof.observed_at, bool) or not math.isfinite(proof.observed_at) or not observation_started_at <= proof.observed_at <= now:
            return None
        if any(not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value for value in (proof.application_path, proof.application_sha256, proof.management_config_path, proof.management_config_sha256, proof.project_id, proof.uid, proof.client_id, proof.grant_id, proof.resource)):
            return None
        management_config = Path(proof.management_config_path)
        if not management_config.is_absolute() or management_config.is_symlink() or (config.management_config is not None and management_config != config.management_config):
            return None
        if hashlib.sha256(management_config.read_bytes()).hexdigest() != proof.management_config_sha256 or hashlib.sha256(Path(proof.application_path).read_bytes()).hexdigest() != proof.application_sha256 or _live(config) != live or time.time() > observation_ends_at:
            return None
        return proof
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None
