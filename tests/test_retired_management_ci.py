"""CI retirement must not silently remove the retained development gates."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_retired_native_management_workflows_are_not_scheduled() -> None:
    workflows = ROOT / ".github/workflows"
    retired_names = {"Management Recovery Epoch Acceptance", "NEW Docker Private Engine Acceptance"}
    for path in workflows.glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        assert workflow["name"] not in retired_names
    assert not (workflows / "management-recovery-epoch.yml").exists()
    assert not (workflows / "new-instance-private-engine.yml").exists()


def test_development_and_mixed_supervisor_gates_remain() -> None:
    for filename in (
        "pr-tests.yml",
        "browser-tests.yml",
        "opencode-live-tests.yml",
        "muse-live-tests.yml",
        "test-source-for-runtime.yml",
        "supervisor-integration-tests.yml",
    ):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        assert "pull_request" in workflow["on"]
        assert workflow["jobs"]
