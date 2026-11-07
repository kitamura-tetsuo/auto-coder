import asyncio
import json
from pathlib import Path

import httpx

from auto_coder.pr_diagnostics import project_pr_diagnostics
from auto_coder.worker_management_bridge import WorkerManagementBridge, configured_identity
from tests.test_worker_management_bridge import ObservedEngine, configure


def _seed(home: Path) -> dict[str, bytes]:
    root = home / ".auto-coder" / "owner/repo"
    root.mkdir(parents=True)
    documents = {
        "pr_review_cycle.json": {"prs": {"17": {"pr_number": 17, "open_epoch": 2, "transition_version": 8, "finding_set_revision": 3, "active_claim": {"claim_id": "claim-1", "round_id": "round-1", "owner_token": "must-not-leak"}, "strong_rounds": [], "closures": []}}},
        "effective_review_decisions.json": {"17": {"pr_number": 17, "head_sha": "a" * 40, "base_sha": "b" * 40, "attempt_id": "attempt-1", "attempt_sequence": 4, "publication": "CONFIRMED", "handoff": "WAITING", "wait_reason": "Bearer secret-token at /private/state", "updated_at": 123.0}},
        "pr_review_effects.json": {
            "operations": {
                "effect-1": {
                    "operation_id": "effect-1",
                    "payload_identity": "payload-1",
                    "repository": "owner/repo",
                    "pr_number": 17,
                    "mode": "ORDINARY_CLOSURE",
                    "round_id": "round-1",
                    "target_head": "a" * 40,
                    "contract_identity": "contract-1",
                    "finding_set_revision": 3,
                    "purpose": "thread-publication",
                    "destination": "thread-9",
                    "exact_payload": "secret payload",
                    "status": "UNCERTAIN",
                    "owner_token": "raw-owner-token",
                    "receipt": "",
                    "reason": "Authorization: Bearer hidden",
                    "updated_at": 124.0,
                }
            }
        },
    }
    for name, document in documents.items():
        (root / name).write_text(json.dumps(document), encoding="utf-8")
    return {name: (root / name).read_bytes() for name in documents}


def test_projection_reads_allowlisted_records_without_mutation_or_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    before = _seed(tmp_path)
    result = project_pr_diagnostics("owner/repo", 17, home=tmp_path)

    assert result["overall_readiness"] is None
    sections = result["sections"]
    assert sections["review_cycle"]["records"][0]["active_claim"]["claim_id"] == "claim-1"
    assert sections["effective_decision"]["records"][0]["publication"] == "CONFIRMED"
    assert sections["review_effects"]["records"][0]["status"] == "UNCERTAIN"
    encoded = json.dumps(result)
    assert "raw-owner-token" not in encoded and "secret payload" not in encoded and "secret-token" not in encoded and "/private/state" not in encoded
    root = tmp_path / ".auto-coder" / "owner/repo"
    assert {name: (root / name).read_bytes() for name in before} == before


def test_private_route_distinguishes_no_match_and_rejects_forged_selector(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path)
    identity = configured_identity("owner/repo")
    assert identity is not None
    bridge = WorkerManagementBridge(identity, ObservedEngine())

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://worker") as client:
            found = await client.get("/v1/pr-diagnostics?pr_number=17&limit=1")
            absent = await client.get("/v1/pr-diagnostics?pr_number=18")
            invalid = await client.get("/v1/pr-diagnostics?pr_number=17&repository=foreign/repo")
        assert found.status_code == 200 and found.headers["cache-control"] == "no-store"
        assert found.json()["sections"]["review_effects"]["records"][0]["operation_id"] == "effect-1"
        assert absent.status_code == 200 and absent.json()["sections"]["review_cycle"]["availability"] == "COMPLETE"
        assert absent.json()["sections"]["review_cycle"]["records"] == []
        assert invalid.status_code == 422

    asyncio.run(scenario())
