"""Construct a prior-journal reader only from a protected fresh intent."""

from __future__ import annotations

import os
from pathlib import Path

import auto_coder

from .bootstrap_supervisor import BootstrapConfig
from .fresh_initialization import load_fresh_initialization, verify_initialization_inputs
from .oauth_config import ManagementSettings
from .retained_journal import RetainedJournal


def retained_from_fresh_intent(settings: ManagementSettings) -> RetainedJournal | None:
    """Fail closed on an incomplete selected retained-history deployment."""
    config_path = settings.bootstrap_config_path
    if config_path is None:
        return None
    bootstrap = BootstrapConfig.read(config_path)
    if bootstrap.fresh_prior_lock_path is None:
        return None
    intent, _ = load_fresh_initialization(bootstrap.state_root)
    if (intent.new_instance, intent.state_root, intent.journal_root, intent.oauth_root) != (bootstrap.instance_id, str(bootstrap.state_root), str(bootstrap.journal_root), str(bootstrap.oauth_root)):
        raise ValueError("fresh management identity differs")
    if settings.config_path is None or settings.state_root != bootstrap.oauth_root or settings.operation_state_root != bootstrap.journal_root:
        raise ValueError("fresh management settings differ")
    runtime = next((item for item in bootstrap.runtimes if item.reference == intent.runtime_reference), None)
    if runtime is None or auto_coder.__file__ is None or bootstrap.fresh_prior_lock_path != Path("/management-prior-owner.lock"):
        raise ValueError("fresh runtime or prior fence unavailable")
    verify_initialization_inputs(intent, bootstrap_config=config_path, management_config=settings.config_path, executable=runtime.executable, application_root=Path(auto_coder.__file__).resolve(strict=True).parent)
    if not intent.host_prior_journal_root:
        return None
    root = Path(intent.prior_journal_root)
    if root.is_symlink() or not root.is_dir() or not os.statvfs(root).f_flag & os.ST_RDONLY:
        raise ValueError("retained journal mount is not read-only")
    reader = RetainedJournal(
        root / "management-operations.sqlite3",
        bootstrap.fresh_prior_lock_path,
        database_identity=(intent.prior_journal_device, intent.prior_journal_inode),
        fence_identity=(intent.owner_lock_device, intent.owner_lock_inode),
        prior_management_instance=intent.old_instance,
    )
    try:
        reader.read()
    except BaseException:
        reader.close()
        raise
    return reader
