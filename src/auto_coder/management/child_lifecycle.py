"""Cooperative management-child shutdown evidence for the stable Supervisor."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from loguru import logger

from .bootstrap_store import BootstrapStore
from .bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor
from .oauth_config import ManagementSettings


def write_management_drain_receipt(settings: ManagementSettings) -> None:
    """Acknowledge completed ASGI shutdown for this exact drained OS lifetime."""
    selected = os.environ.get("AUTO_CODER_DRAIN_RECEIPT")
    if not selected or settings.bootstrap_config_path is None:
        return
    temporary: Path | None = None
    try:
        config = BootstrapConfig.read(settings.bootstrap_config_path)
        record = BootstrapStore(config.state_root, config.instance_id, require_existing=True).load()
        path = Path(selected)
        configured = next((runtime.drain_ack for runtime in config.runtimes if runtime.reference == record.selected_runtime), None)
        if path != (configured or config.state_root / f"drain-{record.selected_runtime}.json"):
            raise ValueError("drain acknowledgement path differs from the configured runtime")
        if record.pid != os.getpid() or record.start_token != ManagementBootstrapSupervisor._start_token(os.getpid()) or str(record.child_generation) != os.environ.get("AUTO_CODER_CHILD_GENERATION") or record.drain_nonce != os.environ.get("AUTO_CODER_DRAIN_NONCE") or not record.intended_stop:
            raise ValueError("drain acknowledgement does not identify this stopped child")
        receipt = {"generation": record.child_generation, "nonce": record.drain_nonce, "pid": record.pid, "start_token": record.start_token}
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".management-drained-", encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(receipt, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, TypeError, KeyError):
        logger.error("Management child drain acknowledgement unavailable")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
