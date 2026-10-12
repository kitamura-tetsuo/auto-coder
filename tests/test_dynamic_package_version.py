from pathlib import Path

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
