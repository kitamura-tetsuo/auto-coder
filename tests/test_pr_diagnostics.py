import asyncio
import json
from pathlib import Path

import httpx

from auto_coder.github_pending_work import WorkIdentity, resolve_pending_work_store
from auto_coder.pr_diagnostics import project_pr_diagnostics
from auto_coder.worker_management_bridge import WorkerManagementBridge, configured_identity
from tests.test_worker_management_bridge import ObservedEngine, configure


def _seed(home: Path) -> dict[str, bytes]:
    root = home / ".auto-coder" / "owner/repo"
    root.mkdir(parents=True)
    documents = {
        "pr_review_cycle.json": {
            "prs": {
                "17": {
                    "pr_number": 17,
                    "open_epoch": 2,
                    "transition_version": 8,
                    "finding_set_revision": 3,
                    "active_claim": {
                        "claim_id": "claim-1",
                        "phase": "STRONG_AUDIT",
                        "head_sha": "a" * 40,
                        "base_sha": "b" * 40,
                        "contract_identity": "contract-1",
                        "policy_identity": "policy-1",
                        "based_on_version": 7,
                        "open_epoch": 2,
                        "owner_token": "must-not-leak",
                    },
                    "strong_rounds": [],
                    "closures": [],
                    "findings": {},
                    "closed": False,
                }
            }
        },
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


def test_private_route_pending_store_absence_exact_pr_filter_and_pagination(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    identity = configured_identity("owner/repo")
    assert identity is not None
    bridge = WorkerManagementBridge(identity, ObservedEngine())

    async def read(path: str) -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://worker") as client:
            response = await client.get(path)
        assert response.status_code == 200
        return response.json()

    missing = asyncio.run(read("/v1/pr-diagnostics?pr_number=17"))
    assert missing["sections"]["pending_work"] == {
        "producer": "PendingWorkStore",
        "availability": "ABSENT",
        "reason": "store_not_present",
        "records": [],
        "truncated": False,
        "next_cursor": None,
    }

    resolution = resolve_pending_work_store("owner/repo", home=tmp_path)
    assert resolution.store is not None
    store = resolution.store
    store.ensure_reevaluation(WorkIdentity("owner/repo", "pr:17", "pr-processing", "rev-a"), ("thread-a",), now=10)
    store.ensure_reevaluation(WorkIdentity("owner/repo", "pr:17", "pr-processing", "rev-b"), ("thread-b",), now=20)
    store.ensure_reevaluation(WorkIdentity("owner/repo", "pr:171", "pr-processing", "other"), ("must-not-join",), now=30)

    first = asyncio.run(read("/v1/pr-diagnostics?pr_number=17&limit=1&cursor=0"))["sections"]["pending_work"]
    revision = asyncio.run(read("/v1/pr-diagnostics?pr_number=17&limit=1&cursor=0"))["evidence_revision"]
    second = asyncio.run(read(f"/v1/pr-diagnostics?pr_number=17&limit=1&cursor=1&expected_revision={revision}"))["sections"]["pending_work"]
    no_match = asyncio.run(read("/v1/pr-diagnostics?pr_number=18"))["sections"]["pending_work"]
    assert first["availability"] == "COMPLETE" and first["records"][0]["identity"]["revision"] == "rev-a"
    assert first["truncated"] is True and first["next_cursor"] == 1
    assert second["records"][0]["identity"]["revision"] == "rev-b"
    assert second["truncated"] is False and second["next_cursor"] is None
    assert no_match["availability"] == "COMPLETE" and no_match["records"] == []
    assert "must-not-join" not in json.dumps((first, second))
