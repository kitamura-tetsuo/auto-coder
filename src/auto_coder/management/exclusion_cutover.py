"""One host session holds ancestor fences across manual stop, claim and grant.

No Docker mutations are performed. A controller interruption never authorizes a
second claim, and an unresolved existing initialization root remains untouched.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

from .archived_exclusions import acquire_exclusion_fences
from .exclusion_host import check_exclusion_peer, check_host_archives, validate_successor
from .fresh_host import _evidence, _protected_file
from .fresh_host_observation import HostObservationUnavailable, _assert_pid_flock, _inspect, assert_host_listener_absent, assert_known_prior_stack_stopped, assert_prior_network_pin, assert_prior_network_unreferenced, assert_retained_journal_source, assert_retained_oauth_mount, serve_one_fresh_grant
from .fresh_initialization import FreshInitializationIntent, acquire_prior_owner_fence, claim_fresh_initialization


def hold_claim_and_guard(
    intent: FreshInitializationIntent, evidence: Path, bootstrap: Path, prior_lock: Path, network_fd: int, guard_dir: Path, *, wait_seconds: float = 300, on_fenced: Callable[[], None] | None = None, on_stopped: Callable[[], None] | None = None, on_listening: Callable[[], None] | None = None
) -> None:
    """Retain separately opened shared locks, never pass process authority."""
    if not math.isfinite(wait_seconds) or not 0 < wait_seconds <= 3600:
        raise ValueError("bounded stop wait required")
    management, proxy, digest = _evidence(evidence)
    if (management.container_id, management.instance_id, management.published_port, digest, management.owner_lock_device, management.owner_lock_inode) != (
        intent.old_container_id,
        intent.old_instance,
        intent.host_listener_port,
        intent.prior_evidence_sha256,
        intent.owner_lock_device,
        intent.owner_lock_inode,
    ):
        raise ValueError("cutover intent differs from captured predecessor")
    assert_retained_oauth_mount(management, intent)
    assert_prior_network_pin(network_fd, management.net_namespace)
    bindings = validate_successor(management, intent, bootstrap)
    with ExitStack() as held:
        ancestors = acquire_exclusion_fences(bindings, host=True)
        for fd in ancestors:
            held.callback(os.close, fd)
        check_exclusion_peer(bindings, ancestors, os.getpid())
        check_host_archives(bindings)
        before = _inspect(management.container_id)
        expected = (management.image_id, True, management.pid, management.started_at, management.restart_count)
        if (before[1], before[2], before[3], before[13], before[14]) != expected:
            raise ValueError("predecessor must still be the captured running lifetime")
        check_exclusion_peer(bindings, ancestors, management.pid)
        owner = os.open(prior_lock, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(owner)
            if (info.st_dev, info.st_ino) != (intent.owner_lock_device, intent.owner_lock_inode):
                raise ValueError("predecessor owner fence changed")
            _assert_pid_flock(management.pid, owner, "WRITE")
        finally:
            os.close(owner)
        after = _inspect(management.container_id)
        if (after[1], after[2], after[3], after[13], after[14]) != expected:
            raise ValueError("predecessor changed during lock handoff observation")
        print(json.dumps({"status": "ancestors-fenced-awaiting-manual-stop", "request_id": intent.request_id}), flush=True)
        if on_fenced is not None:
            on_fenced()
        deadline = time.monotonic() + wait_seconds
        while True:
            values = _inspect(management.container_id)
            if values[1] != management.image_id:
                raise ValueError("predecessor image changed")
            if values[2] is False:
                break
            if values[2] is not True or values[3] != management.pid or values[13] != management.started_at or values[14] != management.restart_count:
                raise ValueError("predecessor lifetime changed")
            if time.monotonic() >= deadline:
                raise TimeoutError("manual predecessor stop not observed")
            time.sleep(0.1)
        direct = acquire_prior_owner_fence(prior_lock, expected_device=intent.owner_lock_device, expected_inode=intent.owner_lock_inode)
        held.callback(os.close, direct)
        assert_known_prior_stack_stopped(management, proxy)
        assert_retained_journal_source(management, intent)
        assert_prior_network_pin(network_fd, management.net_namespace)
        assert_prior_network_unreferenced(management.net_namespace)
        assert_host_listener_absent(intent.host_listener_port)
        if validate_successor(management, intent, bootstrap) != bindings:
            raise ValueError("ancestor set changed across stop")
        check_exclusion_peer(bindings, ancestors, os.getpid())
        check_host_archives(bindings)
        if on_stopped is not None:
            on_stopped()
        claim_fresh_initialization(Path(intent.host_state_root), intent)
        serve_one_fresh_grant(guard_dir / "fresh-guard.sock", intent, management, proxy, direct, network_fd, bootstrap_config=bootstrap, exclusion_fences=ancestors, on_listening=on_listening)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hold archive exclusions through an operator-controlled management cutover; no Docker effects")
    for option in ("intent-file", "evidence-file", "bootstrap-config", "prior-lock", "guard-dir"):
        parser.add_argument("--" + option, required=True, type=Path)
    parser.add_argument("--prior-network-fd", required=True, type=int)
    parser.add_argument("--wait-seconds", type=float, default=300)
    args = parser.parse_args()
    try:
        raw = json.loads(_protected_file(args.intent_file))
        optional = {"host_prior_journal_root", "prior_journal_root", "prior_journal_device", "prior_journal_inode"}
        fields = set(FreshInitializationIntent.__dataclass_fields__)
        if not isinstance(raw, dict) or set(raw) not in (fields, fields - optional):
            raise ValueError("initialization intent fields differ")
        intent = FreshInitializationIntent(**raw)
        intent.validate()
        hold_claim_and_guard(intent, args.evidence_file, args.bootstrap_config, args.prior_lock, args.prior_network_fd, args.guard_dir, wait_seconds=args.wait_seconds)
    except (OSError, ValueError, TypeError, KeyError, HostObservationUnavailable) as error:
        parser.exit(1, "exclusion cutover unavailable: " + str(error) + "\n")


if __name__ == "__main__":
    main()
