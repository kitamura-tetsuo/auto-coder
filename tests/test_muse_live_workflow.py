from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/muse-live-tests.yml"


def test_muse_live_workflow_pins_real_host_and_runs_non_skipped_case() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow["name"] == "Muse Live Tests"
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["muse-live-tests"]
    assert job["name"] == "Muse Live Tests"
    steps = {step["name"]: step for step in job["steps"]}
    install = steps["Install pinned Muse Code host"]["run"]
    assert "version=1.4.6-R6000.1" in install
    assert "82a495542965a5b6256f7acdb81693451c28640a419d5528ad8340ea93f36f6f" in install
    live = steps["Run real-host conformance"]
    assert live["env"]["AUTOCODER_MUSE_CONFORMANCE_BIN"] == "${{ runner.temp }}/muse"
    assert live["run"] == "uv run pytest -m muse_live -vv tests/test_muse_background_live.py"
