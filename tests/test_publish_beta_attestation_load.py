"""Regression for loading provenance-attested images during Publish Beta."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

PUBLISH_BETA_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/publish-beta.yml"


def test_publish_beta_enables_containerd_before_loading_attested_image() -> None:
    workflow = yaml.safe_load(PUBLISH_BETA_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build-tested-artifact"]["steps"]

    def index_of_action(action: str) -> int:
        matches = [i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith(action)]
        assert len(matches) == 1, f"Expected exactly one {action} step, found {len(matches)}"
        return matches[0]

    docker_index = index_of_action("docker/setup-docker-action@")
    login_index = index_of_action("docker/login-action@")
    buildx_index = index_of_action("docker/setup-buildx-action@")
    build_index = index_of_action("docker/build-push-action@")
    verify_index = next(i for i, step in enumerate(steps) if step.get("name") == "Verify and publish tested image")

    assert docker_index < login_index < buildx_index < build_index < verify_index
    daemon_config = json.loads(steps[docker_index]["with"]["daemon-config"])
    assert daemon_config["features"]["containerd-snapshotter"] is True

    build = steps[build_index]["with"]
    assert build["load"] is True
    assert build["provenance"] is True
    assert build["push"] is False
    assert 'docker push "$IMAGE"' in steps[verify_index]["run"]
