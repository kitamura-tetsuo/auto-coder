"""Trusted host operator entry points for one fresh management cutover.

These commands only inspect Docker; starting and stopping services remains an
explicit operator action. Evidence and state must be new protected directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import asdict
from pathlib import Path

import yaml

from .fresh_host_observation import (
    HostObservationUnavailable,
    PriorContainer,
    PriorTask,
    assert_host_listener_absent,
    assert_known_prior_stack_stopped,
    assert_prior_network_pin,
    assert_prior_network_unreferenced,
    assert_retained_journal_source,
    assert_retained_oauth_mount,
    capture_prior_container,
    serve_one_fresh_grant,
)
from .fresh_initialization import FreshInitializationIntent, acquire_prior_owner_fence, claim_fresh_initialization, load_fresh_initialization


def _protected_file(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("protected file path is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("operator input is not protected")
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("operator input is too large")
    return data


def _write_evidence(directory: Path, value: dict[str, object]) -> str:
    if not directory.is_absolute() or directory.is_symlink():
        raise ValueError("guard directory is invalid")
    info = directory.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or any(directory.iterdir()):
        raise PermissionError("guard directory must be empty and owner-only")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open("prior-evidence.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(root)
    finally:
        os.close(root)
    return hashlib.sha256(encoded).hexdigest()


def _prior(value: object) -> PriorContainer:
    if not isinstance(value, dict) or set(value) != set(PriorContainer.__dataclass_fields__):
        raise ValueError("prior evidence fields differ")
    raw = dict(value)
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or any(not isinstance(item, dict) or set(item) != {"pid", "start_token"} for item in tasks):
        raise ValueError("prior task evidence differs")
    raw["tasks"] = tuple(PriorTask(**item) for item in tasks)
    prior = PriorContainer(**raw)
    prior.validate()
    return prior


def _evidence(path: Path) -> tuple[PriorContainer, PriorContainer, str]:
    encoded = _protected_file(path)
    value = json.loads(encoded)
    if not isinstance(value, dict) or set(value) != {"management", "proxy"} or encoded != json.dumps(value, sort_keys=True, separators=(",", ":")).encode():
        raise ValueError("prior evidence is malformed")
    return _prior(value["management"]), _prior(value["proxy"]), hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fresh management host evidence; no Docker mutation")
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="Read-only NEW-instance assessment; never launch authority")
    preflight.add_argument("--request-file", required=True, type=Path)
    begin = commands.add_parser("begin", help="Record a protected NEW origin; no launch or grant")
    begin.add_argument("--request-file", required=True, type=Path)
    inspect = commands.add_parser("inspect-begin", help="Independent read-only verification of a NEW origin")
    inspect.add_argument("--request-file", required=True, type=Path)
    inspect_profile = commands.add_parser("inspect-profile", help="Independent read-only NEW runtime/configuration/OAuth profile verification; no launch")
    inspect_profile.add_argument("--request-file", required=True, type=Path)
    probe_fence = commands.add_parser("probe-admission-fence", help="Probe transient exclusive NEW admission serialization; no launch authority")
    probe_fence.add_argument("--request-file", required=True, type=Path)
    capture = commands.add_parser("capture")
    for option in ("management-id", "management-image", "proxy-id", "proxy-image", "mounts-file", "guard-dir"):
        capture.add_argument("--" + option, required=True)
    claim = commands.add_parser("claim")
    claim.add_argument("--intent-file", required=True, type=Path)
    claim.add_argument("--evidence-file", required=True, type=Path)
    claim.add_argument("--prior-lock", required=True, type=Path)
    claim.add_argument("--prior-network-fd", required=True, type=int)
    claim.add_argument("--bootstrap-config", type=Path)
    guard = commands.add_parser("guard")
    guard.add_argument("--state-root", required=True, type=Path)
    guard.add_argument("--evidence-file", required=True, type=Path)
    guard.add_argument("--prior-lock", required=True, type=Path)
    guard.add_argument("--guard-dir", required=True, type=Path)
    guard.add_argument("--prior-network-fd", required=True, type=int)
    guard.add_argument("--bootstrap-config", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            from .fresh_preflight import assess_new_instance

            print(json.dumps(asdict(assess_new_instance(args.request_file)), sort_keys=True))
        elif args.command == "begin":
            from .fresh_begin import begin_new_instance

            print(json.dumps(asdict(begin_new_instance(args.request_file)), sort_keys=True))
        elif args.command == "inspect-begin":
            from .fresh_begin import inspect_new_instance_origin

            print(json.dumps(asdict(inspect_new_instance_origin(args.request_file)), sort_keys=True))
        elif args.command == "inspect-profile":
            from .fresh_new_profile import inspect_new_instance_profile

            print(json.dumps(asdict(inspect_new_instance_profile(args.request_file)), sort_keys=True))
        elif args.command == "probe-admission-fence":
            from .fresh_new_admission_fence import hold_new_admission_fence

            with hold_new_admission_fence(args.request_file) as observation:
                receipt = asdict(observation)
            print(json.dumps(receipt, sort_keys=True))
        elif args.command == "capture":
            mounts = json.loads(_protected_file(Path(args.mounts_file)))
            if not isinstance(mounts, dict) or set(mounts) != {"management", "proxy"}:
                raise ValueError("approved mount profile is malformed")

            def entries(name: str) -> tuple[tuple[str, str, bool], ...]:
                values = mounts[name]
                if not isinstance(values, list) or any(not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str) or not isinstance(row[1], str) or type(row[2]) is not bool for row in values):
                    raise ValueError("approved mounts are malformed")
                return tuple((row[0], row[1], row[2]) for row in values)

            management = capture_prior_container(args.management_id, args.management_image, expected_mounts=entries("management"), require_owner_lock=True, require_deployment_identity=True)
            proxy = capture_prior_container(args.proxy_id, args.proxy_image, expected_mounts=entries("proxy"))
            if proxy.network_mode != "container:" + management.container_id or proxy.net_namespace != management.net_namespace:
                raise ValueError("proxy is not in the prior management network namespace")
            print(_write_evidence(Path(args.guard_dir), {"management": asdict(management), "proxy": asdict(proxy)}))
        elif args.command == "claim":
            raw = json.loads(_protected_file(args.intent_file))
            optional = {"host_prior_journal_root", "prior_journal_root", "prior_journal_device", "prior_journal_inode"}
            fields = set(FreshInitializationIntent.__dataclass_fields__)
            if not isinstance(raw, dict) or set(raw) not in (fields, fields - optional):
                raise ValueError("fresh initialization intent is malformed")
            intent = FreshInitializationIntent(**raw)
            management, proxy, digest = _evidence(args.evidence_file)
            if (
                management.container_id != intent.old_container_id
                or management.instance_id != intent.old_instance
                or management.published_port != intent.host_listener_port
                or digest != intent.prior_evidence_sha256
                or (management.owner_lock_device, management.owner_lock_inode) != (intent.owner_lock_device, intent.owner_lock_inode)
            ):
                raise ValueError("fresh intent conflicts with prior evidence")
            from .exclusion_host import validate_successor

            if validate_successor(management, intent, args.bootstrap_config):
                raise ValueError("inherited exclusions require the continuous exclusion_cutover session")
            assert_retained_oauth_mount(management, intent)
            assert_prior_network_pin(args.prior_network_fd, management.net_namespace)
            fence = acquire_prior_owner_fence(args.prior_lock, expected_device=intent.owner_lock_device, expected_inode=intent.owner_lock_inode)
            try:
                assert_known_prior_stack_stopped(management, proxy)
                assert_retained_journal_source(management, intent)
                assert_prior_network_unreferenced(management.net_namespace)
                assert_host_listener_absent(intent.host_listener_port)
                print(claim_fresh_initialization(Path(intent.host_state_root), intent))
            finally:
                os.close(fence)
        else:
            intent, _ = load_fresh_initialization(args.state_root, host=True)
            management, proxy, digest = _evidence(args.evidence_file)
            if digest != intent.prior_evidence_sha256:
                raise ValueError("prior evidence differs from fresh intent")
            from .exclusion_host import validate_successor

            if validate_successor(management, intent, args.bootstrap_config):
                raise ValueError("inherited exclusions require the continuous exclusion_cutover session")
            fence = acquire_prior_owner_fence(args.prior_lock, expected_device=intent.owner_lock_device, expected_inode=intent.owner_lock_inode)
            try:
                serve_one_fresh_grant(args.guard_dir / "fresh-guard.sock", intent, management, proxy, fence, args.prior_network_fd, bootstrap_config=args.bootstrap_config)
            finally:
                os.close(fence)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, yaml.YAMLError, HostObservationUnavailable) as exc:
        parser.exit(1, f"fresh host observation unavailable: {exc}\n")


if __name__ == "__main__":
    main()
