"""Read the actual complete package source of a selected local Docker object.

This reader does not execute the package, establish original history, admit a
profile or grant authority. Its caller separately verifies commands and mounts.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import PurePosixPath

_PACKAGE = "/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder"
_MAX_ARCHIVE = 64 * 1024 * 1024
_MAX_SOURCE = 8 * 1024 * 1024
_MAX_FILES = 4096


def _source_archive_identity(encoded: bytes) -> str:
    if not encoded or len(encoded) > _MAX_ARCHIVE:
        raise ValueError("recovery source archive is absent or oversized")
    sources: dict[PurePosixPath, bytes] = {}
    names: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(encoded), mode="r:") as archive:
            for index, member in enumerate(archive):
                if index >= 2 * _MAX_FILES:
                    raise ValueError("recovery source archive has too many entries")
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "auto_coder" or len(member.name) > 4096 or str(path) != member.name.rstrip("/") or str(path) in names:
                    raise ValueError("recovery source archive path is ambiguous")
                names.add(str(path))
                if member.isdir():
                    continue
                if not member.isfile() or member.size < 0:
                    raise ValueError("recovery source archive contains an unsupported entry")
                relative = path.relative_to("auto_coder")
                if relative.suffix in (".pyc", ".pyo", ".so", ".pyd"):
                    raise ValueError("recovery source package contains alternate executable import bytes")
                if relative.suffix != ".py":
                    continue
                if not 0 <= member.size <= _MAX_SOURCE or len(sources) >= _MAX_FILES:
                    raise ValueError("recovery source package exceeds its bound")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("recovery source member is unavailable")
                data = source.read(_MAX_SOURCE + 1)
                if len(data) != member.size:
                    raise ValueError("recovery source member is truncated")
                sources[relative] = data
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ValueError("recovery source archive cannot be decoded") from error
    if not {PurePosixPath("__init__.py"), PurePosixPath("management/bootstrap_supervisor.py"), PurePosixPath("management/original_attempts.py")} <= set(sources):
        raise ValueError("recovery original-history package source is incomplete")
    digest = hashlib.sha256()
    # Match original_attempts.source_identity's Path ordering and byte framing.
    for path in sorted(sources):
        name, content = path.as_posix().encode(), sources[path]
        digest.update(len(name).to_bytes(8, "big") + name + len(content).to_bytes(8, "big") + content)
    return "sha256:" + digest.hexdigest()


def read_image_source_identity(container_id: str) -> str:
    """Hash current package bytes from this exact object on the local engine."""
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("exact recovery source container ID required")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise ValueError("recovery source endpoint override is unsupported")
    context = subprocess.run(("docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"), capture_output=True, text=True, check=True, timeout=10)
    if len(context.stdout) > 4096 or json.loads(context.stdout) != "unix:///var/run/docker.sock":
        raise ValueError("recovery source requires the local system Docker engine")
    archive = subprocess.run(("docker", "cp", container_id + ":" + _PACKAGE, "-"), capture_output=True, check=True, timeout=30)
    return _source_archive_identity(archive.stdout)
