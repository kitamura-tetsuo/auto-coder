"""
Test backward compatibility layer for GraphRAG MCP integration.

This test demonstrates that the backward compatibility layer works correctly
with the new session_id parameter while maintaining backward compatibility.
"""

import os
import sys
import warnings
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from auto_coder.backward_compatibility_layer import (
    BackwardCompatibilityLayer,
    CompatibilityConfig,
    CompatibilityMode,
    get_compatibility_layer,
)


def test_basic_session_id_extraction():
    """Test basic session_id extraction."""
    layer = BackwardCompatibilityLayer()

    # Test with explicit session_id
    session_id, is_legacy = layer.extract_session_id(
        session_id="test_repo_123", repo_path="/test/path"
    )
    assert session_id == "test_repo_123"
    assert not is_legacy
    print("✓ Explicit session_id extraction works")

    # Test with auto-generated session_id
    session_id, is_legacy = layer.extract_session_id(
        session_id=None, repo_path="/test/path"
    )
    assert session_id.startswith("repo_")
    assert is_legacy
    print("✓ Auto-generated session_id works")

    # Test with legacy mode
    config = CompatibilityConfig(mode=CompatibilityMode.LEGACY)
    layer = BackwardCompatibilityLayer(config)
    session_id, is_legacy = layer.extract_session_id(
        session_id=None, repo_path="/test/path"
    )
    assert session_id == "default"
    assert is_legacy
    print("✓ Legacy mode works")


def test_session_id_generation():
    """Test session_id generation from path."""
    layer = BackwardCompatibilityLayer()

    # Same path should generate same session_id
    session_id1 = layer.generate_session_id("/test/path")
    session_id2 = layer.generate_session_id("/test/path")
    assert session_id1 == session_id2

    # Different paths should generate different session_ids
    session_id3 = layer.generate_session_id("/different/path")
    assert session_id3 != session_id1
    print("✓ Session ID generation is consistent")


def test_repo_label_generation():
    """Test repository label generation."""
    layer = BackwardCompatibilityLayer()
    session_id = "test_repo_123"
    label = layer.get_repo_label(session_id)
    assert label.startswith("Session_")
    print(f"✓ Repository label generated: {label}")


def test_deprecation_warnings():
    """Test deprecation warning system."""
    config = CompatibilityConfig(warn_on_legacy=True)
    layer = BackwardCompatibilityLayer(config)

    # Clear any existing warnings
    layer._deprecated_warnings_shown.clear()

    # First warning should be emitted
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        layer._warn("Test warning", category=DeprecationWarning)
        assert len(w) == 1
        assert "Test warning" in str(w[0].message)

    # Second identical warning should not be emitted (deduplicated)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        layer._warn("Test warning", category=DeprecationWarning)
        assert len(w) == 0  # No new warning

    print("✓ Deprecation warning deduplication works")


def test_environment_configuration():
    """Test configuration from environment variables."""
    # Set environment variables
    os.environ["GRAPHRAG_COMPATIBILITY_MODE"] = "legacy"
    os.environ["GRAPHRAG_WARN_ON_LEGACY"] = "false"

    layer = BackwardCompatibilityLayer.from_environment()
    assert layer.config.mode == CompatibilityMode.LEGACY
    assert not layer.config.warn_on_legacy

    # Clean up
    del os.environ["GRAPHRAG_COMPATIBILITY_MODE"]
    del os.environ["GRAPHRAG_WARN_ON_LEGACY"]

    print("✓ Environment configuration works")


def test_global_instance():
    """Test global compatibility layer instance."""
    layer1 = get_compatibility_layer()
    layer2 = get_compatibility_layer()
    assert layer1 is layer2
    print("✓ Global instance works")


if __name__ == "__main__":
    print("Testing Backward Compatibility Layer\n")

    try:
        test_basic_session_id_extraction()
        test_session_id_generation()
        test_repo_label_generation()
        test_deprecation_warnings()
        test_environment_configuration()
        test_global_instance()

        print("\n" + "=" * 50)
        print("All tests passed! ✓")
        print("=" * 50)

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
