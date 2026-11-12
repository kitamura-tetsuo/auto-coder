"""Explicit bootstrap custody endpoint for the one-use recovery issuer.

This stage owns no child launch, durable consumer launch claim, application
readiness or operation admission. Those belong to the separate epoch consumer.
"""

from __future__ import annotations

import fcntl
import os
import signal
import stat
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from auto_coder.logger_config import get_logger, setup_logger

from .original_attempts import identity
from .recovery_epoch_consumer import CONSUMER_CONFIG, connect_recovery_epoch

if TYPE_CHECKING:
    from .bootstrap_supervisor import BootstrapConfig

logger = get_logger(__name__)


class RecoveryBootstrapCancelled(ValueError):
    """Terminate custody immediately; this endpoint never owns a live child."""


def _open_target_owner(config: BootstrapConfig) -> int:
    config.validate()
    if config.recovery_epoch_config != CONSUMER_CONFIG:
        raise ValueError("recovery custody requires its fixed selected bootstrap profile")
    identity(config.state_root, directory=True)
    # The separately authorized preparation must already provide the exact
    # target file. This path cannot create or repair an old or missing owner.
    path = config.state_root / ".supervisor-owner.lock"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        actual = os.fstat(descriptor)
        if identity(path) != (actual.st_dev, actual.st_ino) or not stat.S_ISREG(actual.st_mode) or stat.S_IMODE(actual.st_mode) != 0o600 or actual.st_uid != os.geteuid() or actual.st_nlink != 1:
            raise ValueError("recovery target owner is not the protected original file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if set(os.listdir(config.state_root)) - {".supervisor-owner.lock", "logs"} or os.listdir(config.journal_root):
            raise ValueError("recovery target contains prior or uncertain launch state")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def run_recovery_epoch_bootstrap(config: BootstrapConfig) -> int:
    """Retain acknowledged fences while the authenticated host remains live."""
    owner = -1
    pause = threading.Event()
    previous_handlers: dict[int, object] = {}
    try:
        owner = _open_target_owner(config)
        setup_logger(stream=sys.stdout, log_file=str(config.state_root / "logs" / "recovery-epoch-bootstrap.log"))

        def cancel(*_: object) -> None:
            raise RecoveryBootstrapCancelled("recovery custody was cancelled")

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, cancel)
        with connect_recovery_epoch(CONSUMER_CONFIG, owner, timeout=config.verification_timeout) as consumer:
            logger.bind(recovery_epoch_phase="consumer-custody", current_ready=False, child_started=False).info("Recovery custody acknowledged; no child launch or readiness is established")
            while True:
                consumer.check_current()
                pause.wait(0.2)
    except (OSError, ValueError, TypeError, KeyError) as error:
        logger.bind(recovery_epoch_phase="consumer-unavailable", current_ready=False, child_started=False).error("Recovery custody unavailable: {}", error)
        return 1
    finally:
        for saved_signal, handler in previous_handlers.items():
            signal.signal(saved_signal, handler)  # type: ignore[arg-type]
        if owner >= 0:
            # This endpoint never launches a child, so it has no live child
            # whose containment could outlast these retained fences.
            os.close(owner)
