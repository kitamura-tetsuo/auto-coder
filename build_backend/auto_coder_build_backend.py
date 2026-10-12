"""PEP 517 backend that carries source identity into source distributions."""

from __future__ import annotations

import os
import sys
import importlib.util
from pathlib import Path

from setuptools import build_meta as _setuptools

ROOT = Path.cwd().resolve()
SOURCE = ROOT / "src"
_spec = importlib.util.spec_from_file_location("_auto_coder_build_identity", SOURCE / "auto_coder" / "build_identity.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("Unable to load build identity producer")
_identity_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _identity_module
_spec.loader.exec_module(_identity_module)
resolve_build_identity = _identity_module.resolve_build_identity
serialize_build_identity = _identity_module.serialize_build_identity
deserialize_build_identity = _identity_module.deserialize_build_identity

IDENTITY = ROOT / ".auto-coder-build-identity.json"


def get_build_version():
    supplied = os.environ.get("AUTO_CODER_BUILD_IDENTITY_FILE")
    carried = Path(supplied) if supplied else IDENTITY
    if carried.is_file():
        record = deserialize_build_identity(carried.read_text(encoding="utf-8"))
        # The backend-created carrier is deliberately untracked. Hide only that
        # file while independently observing the checkout so it cannot make an
        # otherwise clean source appear dirty or contradict its own record.
        if carried.resolve() == IDENTITY.resolve():
            payload = IDENTITY.read_bytes()
            IDENTITY.unlink()
            try:
                observed = resolve_build_identity(ROOT)
            finally:
                IDENTITY.write_bytes(payload)
        else:
            observed = resolve_build_identity(ROOT)
        if observed.state != "unknown" and observed != record:
            raise ValueError("Supplied build identity disagrees with the source checkout")
        return record.version
    return resolve_build_identity(ROOT).version


def _with_identity(operation, *args, **kwargs):
    existed = IDENTITY.exists()
    previous = IDENTITY.read_bytes() if existed else None
    if not existed:
        IDENTITY.write_text(serialize_build_identity(resolve_build_identity(ROOT)), encoding="utf-8")
    os.environ["AUTO_CODER_BUILD_IDENTITY_FILE"] = str(IDENTITY)
    try:
        return operation(*args, **kwargs)
    finally:
        os.environ.pop("AUTO_CODER_BUILD_IDENTITY_FILE", None)
        if previous is None:
            IDENTITY.unlink(missing_ok=True)
        else:
            IDENTITY.write_bytes(previous)


def build_sdist(sdist_directory, config_settings=None):
    return _with_identity(_setuptools.build_sdist, sdist_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _with_identity(_setuptools.build_wheel, wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _with_identity(_setuptools.build_editable, wheel_directory, config_settings, metadata_directory)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _with_identity(_setuptools.prepare_metadata_for_build_wheel, metadata_directory, config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _with_identity(_setuptools.prepare_metadata_for_build_editable, metadata_directory, config_settings)


def get_requires_for_build_wheel(config_settings=None):
    return _with_identity(_setuptools.get_requires_for_build_wheel, config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _with_identity(_setuptools.get_requires_for_build_sdist, config_settings)


def get_requires_for_build_editable(config_settings=None):
    return _with_identity(_setuptools.get_requires_for_build_editable, config_settings)
