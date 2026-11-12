#!/usr/bin/env python3
"""Provision configuration for a disposable, nonproduction management MCP topology.

This is an operator-side fixture generator, not a runtime Docker controller.
It never invokes Docker, contacts GitHub, changes a policy grant or starts a worker.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import yaml
from loguru import logger

TEST_REPOSITORY = "kitamura-tetsuo/auto-coder-test"
MANAGER_IMAGE = "auto-coder:management-fixture-17a312445be4"
WORKER_IMAGE = "auto-coder:release-acceptance-065555a6"
SAFE_FIXTURE = re.compile(r"pr[0-9]{3,6}-[a-z0-9-]{6,28}\Z")
SCOPES = ["auto-coder.read", "auto-coder.operate", "auto-coder.deploy"]


def _private_file(path: Path, value: str | bytes) -> None:
    """Create one new fixture-owned file, never overwrite existing state."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value.encode("utf-8") if isinstance(value, str) else value)
        stream.flush()
        os.fsync(stream.fileno())


def _yaml(value: object) -> str:
    return yaml.safe_dump(value, sort_keys=False)


def _source_fixture_revision(source: Path | None) -> str:
    """Accept a clean exact main of the named test-only Git remote or no source."""
    if source is None:
        return ""
    if not (source / "pyproject.toml").is_file() or not (source / "uv.lock").is_file():
        raise ValueError("disposable tested source must contain the real locked Python project")

    def git(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(source), *arguments],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise ValueError("disposable source Git identity is unavailable") from error
        return completed.stdout.strip()

    remote = git("remote", "get-url", "origin")
    if remote not in (
        "https://github.com/kitamura-tetsuo/auto-coder-test.git",
        "git@github.com:kitamura-tetsuo/auto-coder-test.git",
    ):
        raise ValueError("disposable tested source must use the exact test repository remote")
    revision = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or revision != git("rev-parse", "main"):
        raise ValueError("disposable tested source must be the pinned local main commit")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("disposable tested source must have no tracked or untracked changes")
    return revision


def provision(
    *,
    source: Path,
    fixture_id: str,
    firebase_project: str,
    owner_uid: int,
    owner_gid: int,
    test_source: Path | None = None,
) -> Path:
    """Write a fresh topology constrained to the test repository and private volumes."""
    source = source.resolve(strict=True)
    if not (source / "pyproject.toml").is_file() or not (source / "tests/fixtures/supervisor_offline_pid1.py").is_file():
        raise ValueError("source root must be the checked-out Auto-Coder test fixture source")
    if SAFE_FIXTURE.fullmatch(fixture_id) is None:
        raise ValueError("fixture ID must be an exact lowercase disposable identifier")
    if not isinstance(firebase_project, str) or not re.fullmatch(r"fixture-[a-z0-9-]{6,50}", firebase_project):
        raise ValueError("Firebase project ID must name an explicit fixture-only project")
    if owner_uid < 1 or owner_gid < 1:
        raise ValueError("fixture worker cannot run as root")
    test_source = test_source.resolve(strict=True) if test_source is not None else None
    test_sha = _source_fixture_revision(test_source)
    scratch = source / ".agent-tmp"
    if scratch.exists() and (scratch.is_symlink() or not scratch.is_dir()):
        raise ValueError("fixture scratch root must be an ordinary directory")
    scratch.mkdir(mode=0o700, exist_ok=True)
    root = scratch / fixture_id
    root.mkdir(mode=0o700)
    for directory in (
        "secrets",
        "policy",
        "management/oauth",
        "management/operations",
        "beta/runtime/home",
        "beta/runtime/logs",
        "beta/workspace",
        "release/runtime/home",
        "release/runtime/logs",
        "release/workspace",
        "routing",
    ):
        (root / directory).mkdir(parents=True, mode=0o700)
    nonce = secrets.token_hex(16)
    octet = 32 + int(hashlib.sha256(fixture_id.encode()).hexdigest()[:2], 16) % 160
    subnet = f"10.249.{octet}.0/24"
    addresses = {"beta": f"10.249.{octet}.11", "release": f"10.249.{octet}.12"}
    beta_id, release_id = "dot-" + fixture_id + "-beta", "dot-" + fixture_id + "-release"
    release_repo = "fixture/" + fixture_id + "-release"
    project = "dot-" + fixture_id

    for role in ("beta", "release"):
        _private_file(root / "secrets" / f"{role}-bridge", base64.b64encode(os.urandom(48)) + b"\n")
    _private_file(root / "secrets/source-public-read", "")
    _private_file(root / "routing/ownership.json", json.dumps({TEST_REPOSITORY: "beta", release_repo: "release"}))
    for role in ("beta", "release"):
        _private_file(root / role / "runtime/llm_config.toml", '[backend]\norder = ["jules"]\n\n[backends.jules]\nbackend_type = "jules"\nenabled = false\n')
    _private_file(root / "management/oauth/allowed_uids.json", json.dumps({"project_id": firebase_project, "allowed_uids": []}))
    _private_file(
        root / "policy/operating-policy.yaml",
        _yaml(
            {
                "profile": "observe",
                "targets": {
                    beta_id: {"channel": "beta", "repositories": [TEST_REPOSITORY]},
                    release_id: {"channel": "release", "repositories": [release_repo]},
                },
            }
        ),
    )
    _private_file(
        root / "management/config.yaml",
        _yaml(
            {
                "state_root": "/fixture/management/oauth",
                "operation_state_root": "/fixture/management/operations",
                "operating_policy_path": "/fixture/policy/operating-policy.yaml",
                "public_origin": "http://127.0.0.1:8081",
                "bind_host": "127.0.0.1",
                "port": 8081,
                "local_development": True,
                "firebase_project_id": firebase_project,
                "source_repository": TEST_REPOSITORY,
                "source_evidence_token_file": "/fixture/secrets/source-public-read",
                "enabled_scopes": SCOPES,
                "clients": [{"client_id": "dot-disposable-operator", "redirect_uris": ["http://127.0.0.1:49152/callback"], "allowed_scopes": SCOPES}],
            }
        ),
    )

    registry = {
        "workers": [
            {
                "target_id": beta_id if role == "beta" else release_id,
                "supervisor_instance_id": beta_id if role == "beta" else release_id,
                "channel": role,
                "repository": TEST_REPOSITORY if role == "beta" else release_repo,
                "bridge_address": addresses[role],
                "bridge_port": 8843,
                "bridge_secret_file": f"/fixture/secrets/{role}-bridge",
            }
            for role in ("beta", "release")
        ]
    }
    services: dict[str, object] = {}
    for role in ("beta", "release"):
        role_id = beta_id if role == "beta" else release_id
        fixture_mounts = [
            f"{source}:/source:ro",
            f"{source}/tests/fixtures:/fixtures:ro",
        ]
        if test_source is not None:
            fixture_mounts = [f"{source}:/code:ro", f"{source}/tests/fixtures:/fixtures:ro", f"{test_source}:/source:ro"]
        services[role] = {
            "image": WORKER_IMAGE,
            "entrypoint": ["python", "/fixtures/supervisor_offline_pid1.py"],
            "working_dir": "/source",
            "user": f"{owner_uid}:{owner_gid}",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "networks": {"fixture": {"ipv4_address": addresses[role]}},
            "environment": {
                **({} if test_source is not None else {"PYTHONPATH": "/source/src"}),
                "HOME": "/runtime/home",
                "AUTO_CODER_SUPERVISOR_INSTANCE_ID": role_id,
                "AUTO_CODER_CHANNEL": role,
                "AUTO_CODER_REPOSITORY": TEST_REPOSITORY if role == "beta" else release_repo,
                "AUTO_CODER_RUNTIME_ROOT": "/runtime",
                "AUTO_CODER_WORKSPACE_ROOT": "/workspace",
                "AUTO_CODER_OWNERSHIP_FILE": "/fixture/routing/ownership.json",
                "AUTO_CODER_CONFIG_PATH": "/runtime/llm_config.toml",
                "AUTO_CODER_ARTIFACT_DIGEST": "disposable-fixture-only",
                "AUTO_CODER_BRIDGE_BIND_ADDRESS": addresses[role],
                "AUTO_CODER_BRIDGE_PORT": "8843",
                "AUTO_CODER_BRIDGE_SECRET_FILE": f"/fixture/secrets/{role}-bridge",
                "AUTO_CODER_SUPERVISOR_AUTO_START": "1",
                "AUTO_CODER_FIXTURE_HOLD_BOOTSTRAP": "1",
                "AUTO_CODER_TEST_REQUEST_AUDIT": "1",
                "AUTO_CODER_DISABLE_AUTO_UPDATE": "1",
                "AUTO_CODER_HEALTH_LOG_ENABLED": "0",
                "AUTO_CODER_SOURCE_REPOSITORY_ROOT": "/source",
                "GITHUB_TOKEN": "nonfunctional-disposable-no-github-write",
            },
            "volumes": [
                *fixture_mounts,
                f"{root}/secrets/{role}-bridge:/fixture/secrets/{role}-bridge:ro",
                # The real worker acquires an ownership.json.lock in this
                # fixture-private directory; keep only this test mount writable.
                f"{root}/routing:/fixture/routing:rw",
                f"{root}/{role}/runtime:/runtime",
                f"{root}/{role}/workspace:/workspace",
            ],
            "restart": "no",
        }
    services["management"] = {
        "image": MANAGER_IMAGE,
        "entrypoint": ["python", "-m", "auto_coder.cli", "management", "serve", "--config", "/fixture/management/config.yaml"],
        "working_dir": "/source",
        "user": f"{owner_uid}:{owner_gid}",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "networks": {"fixture": {"ipv4_address": f"10.249.{octet}.10"}},
        "environment": {
            "PYTHONPATH": "/source/src",
            "HOME": "/fixture/management/oauth",
            "DOT_DISPOSABLE_FIXTURE_NONCE": nonce,
            "AUTO_CODER_WORKER_REGISTRY": json.dumps(registry, separators=(",", ":")),
        },
        "volumes": [
            f"{source}:/source:ro",
            f"{root}/management:/fixture/management",
            f"{root}/policy:/fixture/policy:ro",
            f"{root}/secrets/beta-bridge:/fixture/secrets/beta-bridge:ro",
            f"{root}/secrets/release-bridge:/fixture/secrets/release-bridge:ro",
            f"{root}/secrets/source-public-read:/fixture/secrets/source-public-read:ro",
            f"{root}/fixture.json:/fixture/fixture.json:ro",
        ],
        "restart": "no",
        "depends_on": ["beta", "release"],
    }
    _private_file(
        root / "compose.yaml",
        _yaml(
            {
                "name": project,
                "services": services,
                "networks": {"fixture": {"driver": "bridge", "ipam": {"config": [{"subnet": subnet}]}}},
            }
        ),
    )
    _private_file(
        root / "fixture.json",
        json.dumps(
            {
                "schema": 1,
                "fixture_nonce": nonce,
                "firebase_project_id": firebase_project,
                "repository": TEST_REPOSITORY,
                "fixture_id": fixture_id,
                "compose_project": project,
                "subnet": subnet,
                "endpoint": "http://127.0.0.1:8081/mcp",
                "endpoint_namespace": "management-container-only",
                "target_ids": {"beta": beta_id, "release": release_id},
                "mutation_policy": "observe until separately configured and granted",
                "docker_authority_in_worker_or_management": False,
                "source_fixture_sha": test_sha,
                "source_fixture_available": bool(test_sha),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--firebase-project-id", required=True)
    parser.add_argument("--test-source", type=Path, help="Clean main of the explicitly approved test-only Git repository")
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stdout)
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auto-coder"
    state.mkdir(parents=True, exist_ok=True)
    logger.add(state / "dot-disposable-fixture.log", rotation="2 MB", retention=3)
    try:
        root = provision(
            source=args.source,
            fixture_id=args.fixture_id,
            firebase_project=args.firebase_project_id,
            owner_uid=args.uid,
            owner_gid=args.gid,
            test_source=args.test_source,
        )
    except (OSError, ValueError) as error:
        logger.error("No fixture created: {}", type(error).__name__)
        return 2
    print(json.dumps({"verdict": "NOT_RUN", "compose_path": str(root / "compose.yaml"), "manifest_path": str(root / "fixture.json"), "reason": "Provisioned private fixture configuration; no Docker or authenticated effect executed"}))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
