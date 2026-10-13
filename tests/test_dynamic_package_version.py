import importlib.util
import sys
import types
from pathlib import Path

import pytest

from auto_coder.build_identity import resolve_build_identity
from auto_coder.build_version import get_build_version


def test_build_version_matches_checkout_identity(monkeypatch):
    monkeypatch.delenv("AUTO_CODER_BUILD_IDENTITY_FILE", raising=False)
    root = Path(__file__).resolve().parents[1]
    assert get_build_version() == resolve_build_identity(root).version


def test_runtime_version_uses_distribution_metadata():
    from importlib.metadata import version

    import auto_coder

    assert auto_coder.__version__ == version("auto-coder")


def test_editable_build_rejects_source_changed_after_metadata(tmp_path, monkeypatch):
    backend_path = Path(__file__).resolve().parents[1] / "build_backend" / "auto_coder_build_backend.py"
    spec = importlib.util.spec_from_file_location("tested_build_backend", backend_path)
    assert spec and spec.loader
    backend = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = backend
    setuptools = types.ModuleType("setuptools")
    hook = lambda *args, **kwargs: None
    setuptools.build_meta = types.SimpleNamespace(
        build_sdist=hook,
        build_wheel=hook,
        build_editable=hook,
        prepare_metadata_for_build_wheel=hook,
        prepare_metadata_for_build_editable=hook,
        get_requires_for_build_wheel=hook,
        get_requires_for_build_sdist=hook,
        get_requires_for_build_editable=hook,
    )
    monkeypatch.setitem(sys.modules, "setuptools", setuptools)
    spec.loader.exec_module(backend)

    identity_a = backend._identity_module.BuildIdentity(state="clean", version=f"2026.1.1.1+g{'b' * 40}", source_revision="b" * 40, source_date_epoch=1767225600, revision_count=1, reason=None)
    identity_b = backend._identity_module.BuildIdentity(state="clean", version=f"2026.1.1.1+g{'a' * 40}", source_revision="a" * 40, source_date_epoch=1767225600, revision_count=1, reason=None)
    metadata = tmp_path / "metadata" / "auto_coder.dist-info"
    metadata.mkdir(parents=True)
    (metadata / backend.PREPARED_IDENTITY).write_text(backend.serialize_build_identity(identity_a), encoding="utf-8")
    monkeypatch.setattr(backend, "_selected_identity", lambda: identity_b)

    with pytest.raises(ValueError, match="Source identity changed"):
        backend.build_editable(str(tmp_path / "wheel"), metadata_directory=str(tmp_path / "metadata"))

    malformed = tmp_path / "malformed.json"
    malformed.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("AUTO_CODER_BUILD_IDENTITY_FILE", str(malformed))
    called = False

    def operation():
        nonlocal called
        called = True

    with pytest.raises(backend._identity_module.BuildIdentityError, match="Malformed"):
        backend._with_identity(operation)
    assert called is False
