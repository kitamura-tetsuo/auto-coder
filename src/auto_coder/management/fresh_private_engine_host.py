"""Explicit trusted-local host CLI for a separate NEW Docker Engine.

Do not run this inside the management container. Preparing this parent has
host-level Docker privileges and is distinct from granting OAuth/MCP control.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path

from loguru import logger

from .fresh_preflight import _open, _path
from .fresh_private_engine import create_private_engine, hold_private_engine_source


def _configure_logs(path: Path) -> None:
    """Require an existing protected log directory outside the source root."""
    _path(str(path))
    with _open(path.parent, directory=True) as descriptor:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("NEW private Engine logs require an owner-only host directory")
    try:
        target = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(target.st_mode) or target.st_uid != os.geteuid() or target.st_nlink != 1 or target.st_mode & 0o077:
            raise PermissionError("NEW private Engine existing log file must be owner-only regular data")
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.remove()
    logger.add(sys.stdout, colorize=sys.stdout.isatty(), format=layout)
    previous_umask = os.umask(0o077)
    try:
        logger.add(path, rotation="10 MB", retention="5 days", format=layout)
    finally:
        os.umask(previous_umask)


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit host-only NEW private Docker Engine authority")
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--outer-socket", type=Path, default=Path("/var/run/docker.sock"))
    parser.add_argument("--log-file", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Consume a one-way origin and create one isolated Docker-in-Docker parent")
    prepare.add_argument("--parent-image-id", required=True)
    prepare.add_argument("--timeout", type=float, default=60)
    commands.add_parser("inspect", help="Inspect the held original parent; do not create/start a container")
    args = parser.parse_args()
    try:
        private_root = _path(str(args.private_root))
        log_file = _path(str(args.log_file))
        if log_file == private_root or private_root in log_file.parents:
            raise ValueError("NEW private Engine logs must not occupy the one-way source root")
        _configure_logs(log_file)
        if args.command == "prepare":
            evidence = create_private_engine(args.request_file, args.private_root, args.outer_socket, args.parent_image_id, timeout=args.timeout)
        else:
            with hold_private_engine_source(args.request_file, args.private_root, args.outer_socket) as evidence:
                pass
        logger.bind(request_id=evidence.request_id, source=evidence.private_root).info("Private NEW Engine original verified")
        print(json.dumps({"status": "source-verified-only", "launch_authorized": False, "evidence": asdict(evidence)}, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError) as error:
        logger.exception("Private NEW Engine host operation refused: {}", error)
        parser.exit(1, f"private NEW Engine authority unavailable: {error}\n")


if __name__ == "__main__":
    main()
