"""CLI safety and image selection for the explicitly authorized host fixture.

These parser tests do not execute Docker or grant a containment verdict.
"""

from __future__ import annotations

import sys

import pytest
from scripts import test_management_containment_enrollment_host as fixture

REVIEWED_SOURCE_SHA = "66bcfefa86aa2bd88dd4a253405fc6faac65675917e8d393e587d5a62fff83bb"


def test_host_fixture_requires_explicit_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["host-fixture", "--test-image", "sha256:" + "a" * 64])
    with pytest.raises(SystemExit) as raised:
        fixture.main()
    assert raised.value.code == 2


def test_host_fixture_passes_exact_test_image_to_authorized_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, object] = {}

    def selected(*, exercise_test_route_manifest: bool, test_image: str, require_reviewed_ordinary_source: bool) -> None:
        options.update(route_manifest=exercise_test_route_manifest, image=test_image, reviewed_source=require_reviewed_ordinary_source)

    monkeypatch.setattr(fixture, "run_fixture", selected)
    selected_image = "sha256:" + "b" * 64
    monkeypatch.setattr(sys, "argv", ["host-fixture", "--confirm-test-only-fixture", "--exercise-test-route-manifest", "--require-reviewed-ordinary-source", "--test-image", selected_image])
    fixture.main()
    assert options == {"route_manifest": True, "image": selected_image, "reviewed_source": True}


def test_host_fixture_keeps_default_preinstalled_image(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, object] = {}

    def selected(*, exercise_test_route_manifest: bool, test_image: str, require_reviewed_ordinary_source: bool) -> None:
        options.update(route_manifest=exercise_test_route_manifest, image=test_image, reviewed_source=require_reviewed_ordinary_source)

    monkeypatch.setattr(fixture, "run_fixture", selected)
    monkeypatch.setattr(sys, "argv", ["host-fixture", "--confirm-test-only-fixture"])
    fixture.main()
    assert options == {"route_manifest": False, "image": fixture.IMAGE_TAG, "reviewed_source": False}


def test_reviewed_source_requires_actual_image_readback() -> None:
    with pytest.raises(AssertionError, match="reviewed original-source readback is unavailable"):
        fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": None, "unavailable": [{"authority": "installed-bootstrap-source", "reason": "absent"}]})
    with pytest.raises(AssertionError, match="contradictory refusal"):
        fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": {"reviewed_lock_before_launch": True, "source_sha256": REVIEWED_SOURCE_SHA, "source_bytes": 40}, "unavailable": [{"authority": "installed-bootstrap-source", "reason": "contradiction"}]})
    with pytest.raises(AssertionError, match="reviewed original-source readback is unavailable"):
        fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": {"reviewed_lock_before_launch": True, "source_sha256": "0" * 64, "source_bytes": 40}, "unavailable": []})
    fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": {"reviewed_lock_before_launch": True, "source_sha256": REVIEWED_SOURCE_SHA, "source_bytes": 40}, "unavailable": []})
