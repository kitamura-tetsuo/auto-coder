"""PEP 517 backend that carries source identity into source distributions."""

from __future__ import annotations

import email.parser
import importlib.util
import os
import sys
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
PREPARED_IDENTITY = "auto-coder-build-identity.json"


def _selected_identity():
    carried = Path(os.environ.get("AUTO_CODER_BUILD_IDENTITY_FILE", IDENTITY))
    return deserialize_build_identity(carried.read_text(encoding="utf-8")) if carried.is_file() else resolve_build_identity(ROOT)


def _assert_metadata_version(record):
    pkg_info = ROOT / "PKG-INFO"
    if pkg_info.is_file():
        message = email.parser.Parser().parsestr(pkg_info.read_text(encoding="utf-8"))
        if message.get("Version") != record.version:
            raise ValueError("Carried build identity disagrees with sdist PKG-INFO")


def get_build_version():
    supplied = os.environ.get("AUTO_CODER_BUILD_IDENTITY_FILE")
    carried = Path(supplied) if supplied else IDENTITY
    if carried.is_file():
        record = deserialize_build_identity(carried.read_text(encoding="utf-8"))
        _assert_metadata_version(record)
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
    supplied = os.environ.get("AUTO_CODER_BUILD_IDENTITY_FILE")
    if supplied:
        # Validate the caller-selected record (including checkout agreement)
        # without replacing its path with a generated carrier.
        get_build_version()
        return operation(*args, **kwargs)
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
    _assert_prepared_identity(metadata_directory)
    return _with_identity(_setuptools.build_wheel, wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _assert_prepared_identity(metadata_directory)
    return _with_identity(_setuptools.build_editable, wheel_directory, config_settings, metadata_directory)


def _assert_prepared_identity(metadata_directory):
    if not metadata_directory:
        return
    metadata_root = Path(metadata_directory)
    candidates = [metadata_root / PREPARED_IDENTITY, *metadata_root.glob(f"*.dist-info/{PREPARED_IDENTITY}")]
    captured = next((candidate for candidate in candidates if candidate.is_file()), None)
    if captured is None or deserialize_build_identity(captured.read_text(encoding="utf-8")) != _selected_identity():
        raise ValueError("Source identity changed after metadata preparation")


def _prepare_metadata(operation, metadata_directory, config_settings):
    result = _with_identity(operation, metadata_directory, config_settings)
    record = _selected_identity()
    (Path(metadata_directory) / result / PREPARED_IDENTITY).write_text(serialize_build_identity(record), encoding="utf-8")
    return result


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _prepare_metadata(_setuptools.prepare_metadata_for_build_wheel, metadata_directory, config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _prepare_metadata(_setuptools.prepare_metadata_for_build_editable, metadata_directory, config_settings)


def get_requires_for_build_wheel(config_settings=None):
    return _with_identity(_setuptools.get_requires_for_build_wheel, config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _with_identity(_setuptools.get_requires_for_build_sdist, config_settings)


def get_requires_for_build_editable(config_settings=None):
    return _with_identity(_setuptools.get_requires_for_build_editable, config_settings)
