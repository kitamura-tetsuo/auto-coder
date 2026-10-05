"""
Read-only observation of the running Auto-Coder controller artifact.

The production Docker build embeds its trusted ``AUTO_CODER_SOURCE_REVISION``
build input into a small JSON record inside the installed ``auto_coder``
package (see ``embed_from_environment``). A fresh controller process resolves
that record from the installation being executed, never from the working
directory, the target repository, Git, GitHub, Docker or the environment.

Reading is purely diagnostic: it performs no network access, spawns no
process, never raises, and exposes no filesystem paths or environment
contents. Unavailable values are explicit ``None`` with a reason, never
guessed.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

OBSERVATION_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
RECORD_FILENAME = "build_provenance.json"
DISTRIBUTION_NAME = "auto-coder"
SOURCE_REVISION_ENV = "AUTO_CODER_SOURCE_REVISION"

ORIGIN_BUILD_EMBEDDED = "build_embedded"
ORIGIN_INSTALLED_DISTRIBUTION = "installed_distribution_metadata"
ORIGIN_CONTROLLER_PROCESS = "controller_process"
ORIGIN_NONE = "none"

REASON_MISSING = "embedded_record_missing"
REASON_UNREADABLE = "embedded_record_unreadable"
REASON_MALFORMED = "embedded_record_malformed"
REASON_UNSUPPORTED = "embedded_record_unsupported_schema"
REASON_REVISION_ABSENT = "source_revision_absent"
REASON_REVISION_INVALID = "source_revision_invalid"
REASON_VERSION_UNAVAILABLE = "distribution_not_installed"
REASON_PROCESS_RUN_UNAVAILABLE = "process_run_id_unavailable"

_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True)
class ArtifactField:
    """One observed value with its provenance origin and availability."""

    value: Optional[str] = None
    origin: str = ORIGIN_NONE
    available: bool = False
    reason: Optional[str] = None


@dataclass(frozen=True)
class ControllerArtifactObservation:
    """Versioned, read-only description of the running controller artifact."""

    schema_version: int = OBSERVATION_SCHEMA_VERSION
    process_run_id: Optional[str] = None
    process_run: ArtifactField = ArtifactField(reason=REASON_PROCESS_RUN_UNAVAILABLE)
    distribution_version: ArtifactField = ArtifactField(reason=REASON_VERSION_UNAVAILABLE)
    source_revision: ArtifactField = ArtifactField(reason=REASON_MISSING)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_source_revision(raw: object) -> Optional[str]:
    """Return a lowercase full commit SHA, or ``None`` when ``raw`` is not one."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    return candidate if _FULL_SHA_RE.match(candidate) else None


def _record_path() -> Path:
    return Path(__file__).resolve().parent / RECORD_FILENAME


def _read_source_revision() -> ArtifactField:
    path = _record_path()
    try:
        if not path.is_file():
            return ArtifactField(reason=REASON_MISSING)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ArtifactField(reason=REASON_MALFORMED)
    except (OSError, UnicodeDecodeError):
        return ArtifactField(reason=REASON_UNREADABLE)
    if not isinstance(payload, dict):
        return ArtifactField(reason=REASON_MALFORMED)
    if payload.get("schema_version") != RECORD_SCHEMA_VERSION:
        return ArtifactField(reason=REASON_UNSUPPORTED)
    if "source_revision" not in payload:
        return ArtifactField(reason=REASON_REVISION_ABSENT)
    revision = normalize_source_revision(payload["source_revision"])
    if revision is None:
        return ArtifactField(reason=REASON_REVISION_INVALID)
    return ArtifactField(value=revision, origin=ORIGIN_BUILD_EMBEDDED, available=True)


def _read_distribution_version() -> ArtifactField:
    try:
        version = importlib.metadata.version(DISTRIBUTION_NAME)
    except Exception:
        return ArtifactField(reason=REASON_VERSION_UNAVAILABLE)
    if not version:
        return ArtifactField(reason=REASON_VERSION_UNAVAILABLE)
    return ArtifactField(value=version, origin=ORIGIN_INSTALLED_DISTRIBUTION, available=True)


def _read_process_run_id() -> ArtifactField:
    try:
        from .execution_trace import get_trace_collector

        run_id = get_trace_collector().process_run_id
    except Exception:
        return ArtifactField(reason=REASON_PROCESS_RUN_UNAVAILABLE)
    if not isinstance(run_id, str) or not run_id:
        return ArtifactField(reason=REASON_PROCESS_RUN_UNAVAILABLE)
    return ArtifactField(value=run_id, origin=ORIGIN_CONTROLLER_PROCESS, available=True)


def observe_controller_artifact() -> ControllerArtifactObservation:
    """Observe the running artifact. Never raises and has no side effects."""
    try:
        process_run = _read_process_run_id()
        return ControllerArtifactObservation(
            process_run_id=process_run.value,
            process_run=process_run,
            distribution_version=_read_distribution_version(),
            source_revision=_read_source_revision(),
        )
    except Exception:
        return ControllerArtifactObservation()


def embed_from_environment(env: Optional[dict[str, str]] = None) -> int:
    """Write the embedded record from the build input; used by the Docker build.

    An unset or invalid input (e.g. the ``unknown`` default) is recorded as a
    null revision rather than a guessed value.
    """
    source = os.environ if env is None else env
    revision = normalize_source_revision(source.get(SOURCE_REVISION_ENV, ""))
    record = {"schema_version": RECORD_SCHEMA_VERSION, "source_revision": revision}
    _record_path().write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "observe"
    if command == "embed":
        raise SystemExit(embed_from_environment())
    print(json.dumps(observe_controller_artifact().to_dict(), sort_keys=True))
