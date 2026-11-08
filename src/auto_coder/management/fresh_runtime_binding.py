"""Versioned fresh installed-runtime provenance, sealed by the bootstrap hash.

The existing initialization intent remains byte-for-byte unchanged. Its
application digest authenticates the immutable bootstrap closure; this binding
separately authenticates the initial installed child and later selected READYs.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.worker_versions import WorkerVersionError

if TYPE_CHECKING:
    from .bootstrap_store import ChildRecord
    from .bootstrap_supervisor import BootstrapConfig, RuntimeSpec
    from .fresh_initialization import FreshInitializationIntent
    from .oauth_config import ManagementSettings


@dataclass(frozen=True)
class FreshRuntimeBinding:
    version: int
    bootstrap_application_root: str
    initial_ready_path: str
    initial_ready_identity: str

    def validate(self) -> None:
        if type(self.version) is not int or self.version != 2:
            raise ValueError("unsupported fresh runtime binding version")
        for value in (self.bootstrap_application_root, self.initial_ready_path):
            if not isinstance(value, str) or not 0 < len(value) <= 4096 or "\x00" in value or not Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError("invalid fresh runtime binding path")
        if not isinstance(self.initial_ready_identity, str) or len(self.initial_ready_identity) != 64 or any(c not in "0123456789abcdef" for c in self.initial_ready_identity):
            raise ValueError("invalid initial management READY identity")


def parse_fresh_runtime_binding(raw: object) -> FreshRuntimeBinding | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != set(FreshRuntimeBinding.__dataclass_fields__):
        raise ValueError("fresh runtime binding fields differ")
    binding = FreshRuntimeBinding(**raw)
    binding.validate()
    return binding


def fresh_management_path(runtime: RuntimeSpec) -> Path:
    if runtime.execution_kind == "management-installed":
        if len(runtime.arguments) != 8 or runtime.arguments[:7] != ("-I", "-B", "-m", "auto_coder.cli", "management", "serve", "--config"):
            raise ValueError("fresh installed invocation differs")
        result = Path(runtime.arguments[7])
    elif runtime.execution_kind == "source-script" and len(runtime.arguments) == 4 and runtime.arguments[:3] == ("management", "serve", "--config"):
        result = Path(runtime.arguments[3])
    else:
        raise ValueError("fresh management invocation differs")
    if not result.is_absolute():
        raise ValueError("fresh management configuration is not absolute")
    return result


def verify_installed_fresh_binding(config: BootstrapConfig, config_path: Path, intent: FreshInitializationIntent, settings: ManagementSettings, *, source_authority: TestedSourcePublisher | None = None, bootstrap_application: Path | None = None, serving_process: bool = False) -> RuntimeSpec:
    """Revalidate sealed original provenance and optionally the actual live child.

    No store, READY, intent, receipt or runtime selection is created here.
    External source authority is supplied only by trusted controller construction.
    """
    from .configured_stager import configured_management_stager
    from .fresh_initialization import application_tree_sha256
    from .self_update_handoff import runtime_for_receipt
    from .self_update_preflight import ManagementPreflightError

    binding = config.fresh_runtime_binding
    if binding is None:
        raise ValueError("installed fresh binding is missing")
    binding.validate()
    intent.validate()
    initial = next((item for item in config.runtimes if item.reference == config.initial_runtime), None)
    if initial is None or initial.execution_kind != "management-installed" or initial.reference != intent.runtime_reference or config.instance_id != intent.new_instance:
        raise ValueError("initial installed runtime differs")
    management = fresh_management_path(initial)
    if settings.bootstrap_config_path != config_path or settings.config_path != management or config.management_config != management or settings.state_root != config.oauth_root or settings.operation_state_root != config.journal_root:
        raise ValueError("installed fresh settings differ")
    if (str(config.state_root), str(config.journal_root), str(config.oauth_root)) != (intent.state_root, intent.journal_root, intent.oauth_root):
        raise ValueError("installed fresh roots differ")
    for path, expected in ((config_path, intent.bootstrap_config_sha256), (management, intent.management_config_sha256)):
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("installed fresh input differs from intent")
    anchor = Path(binding.bootstrap_application_root)
    if anchor != anchor.resolve(strict=True) or not anchor.is_dir() or not os.statvfs(anchor).f_flag & os.ST_RDONLY:
        raise ValueError("bootstrap application anchor is not immutable")
    if bootstrap_application is not None and anchor != bootstrap_application.resolve(strict=True):
        raise ValueError("running bootstrap application differs from anchor")
    if application_tree_sha256(anchor) != intent.application_sha256:
        raise ValueError("immutable bootstrap application differs")
    # The bootstrap closure is permanently read-only and independent of every
    # mutable child installation and operation/OAuth store. Child READYs retain
    # the existing stager's authenticated content contract, not a new remount.
    for mutable in (config.management_version_root, config.state_root, config.journal_root, config.oauth_root):
        if mutable is None:
            raise ValueError("installed fresh mutable root is unavailable")
        resolved = mutable.resolve(strict=True)
        if anchor == resolved or anchor in resolved.parents or resolved in anchor.parents:
            raise ValueError("bootstrap anchor overlaps mutable runtime or state")
    _verify_environment(initial)
    if initial.ready_receipt != Path(binding.initial_ready_path) or initial.source_receipt != binding.initial_ready_identity:
        raise ValueError("initial installed READY binding differs")
    try:
        stager = configured_management_stager(settings, config, source_authority=source_authority)
        ready = stager.lookup(Path(binding.initial_ready_path))
        if ready.receipt_identity != binding.initial_ready_identity or ready.instance_id != config.instance_id or ready.configuration_sha256 != intent.management_config_sha256 or initial != runtime_for_receipt(config, ready):
            raise ValueError("initial installed profile is not the authenticated READY")
        if ready.python_executable_sha256 != intent.executable_sha256 or hashlib.sha256(initial.executable.read_bytes()).hexdigest() != intent.executable_sha256:
            raise ValueError("initial installed Python differs")
        if serving_process:
            live = _serving_live(config)
            child, selected = live
            if selected.execution_kind != "management-installed" or selected.ready_receipt is None:
                raise ValueError("selected child is not management-installed")
            _verify_environment(selected)
            current = stager.lookup(selected.ready_receipt)
            if current.instance_id != config.instance_id or selected != runtime_for_receipt(config, current) or (child.runtime_receipt, child.source_receipt) != (selected.receipt, current.receipt_identity):
                raise ValueError("selected child differs from its authenticated READY")
            import auto_coder

            if auto_coder.__file__ is None or not Path(auto_coder.__file__).resolve(strict=True).is_relative_to(Path(current.environment_path).resolve(strict=True)):
                raise ValueError("serving application is outside the authenticated selected environment")
    except (WorkerVersionError, ManagementPreflightError) as error:
        raise ValueError("authenticated installed fresh evidence unavailable") from error
    return initial


def _verify_environment(runtime: RuntimeSpec) -> None:
    safe_python = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1"}
    if any((key.startswith("PYTHON") and safe_python.get(key) != value) or key.startswith("LD_") for key, value in runtime.environment):
        raise ValueError("installed fresh environment contains code hooks")


def _serving_live(config: BootstrapConfig) -> tuple[ChildRecord, RuntimeSpec]:
    """Wait for Popen's durable RUNNING publication; never accept another child.

    The parent reserves a generation/nonce before Popen and publishes the PID
    immediately afterwards. A fast child may begin importing before that write.
    Waiting cannot authorize a process: every successful result still requires
    the complete kernel PID/start-token, argv, environment and ledger proof.
    """
    from .readiness_observation import _live
    from .self_update_handoff import observe_supervisor

    generation = os.environ.get("AUTO_CODER_CHILD_GENERATION")
    nonce = os.environ.get("AUTO_CODER_DRAIN_NONCE")
    if not generation or not nonce:
        raise ValueError("serving child launch identity is missing")
    deadline = time.monotonic() + 2
    while True:
        observed = observe_supervisor(config)
        if observed is not None and (observed[1].pid != os.getpid() or str(observed[1].child_generation) != generation or observed[1].drain_nonce != nonce):
            raise ValueError("serving child launch identity differs")
        live = _live(config)
        if live is not None:
            if live[0].pid != os.getpid() or str(live[0].child_generation) != generation or live[0].drain_nonce != nonce:
                raise ValueError("serving child is not the selected live process")
            return live
        if time.monotonic() >= deadline:
            raise ValueError("serving child live proof is unavailable")
        time.sleep(0.01)
