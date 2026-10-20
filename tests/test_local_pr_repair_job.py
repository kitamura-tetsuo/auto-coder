from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.ci_repair_authority import CIRepairAuthority
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobStore
from auto_coder.local_pr_repair_job import LocalPRRepairOffer, offer_local_pr_repair_job
from auto_coder.pr_repair_dispatch import RepairKind

pytestmark = pytest.mark.pr_repair_contract


def snapshot(
    conclusion: CIConclusion = CIConclusion.FAILURE,
    availability: ObservationAvailability = ObservationAvailability.KNOWN,
) -> CIObservationSnapshot:
    facts = () if availability is ObservationAvailability.KNOWN_EMPTY else (WorkflowObservation(WorkflowExecutionIdentity("101", "202", 1), conclusion),)
    return CIObservationSnapshot(
        ObservationSubject("https://api.github.com", "owner/repo", 17, "H1"),
        ObservationRequest("github-actions", "complete"),
        "cycle-1",
        0,
        availability,
        facts,
    )


def request(kind: RepairKind = RepairKind.CI_FAILURE, **changes: str) -> LocalPRRepairOffer:
    values = {
        "repository": "owner/repo",
        "pr_number": 17,
        "head_repository": "owner/repo",
        "head_ref": "feature",
        "head_sha": "H1",
        "base_repository": "owner/repo",
        "base_ref": "main",
        "base_identity": "B1",
        "kind": kind,
        "backend_name": "codex",
        "upstream_attempt": "claim-4",
        "upstream_incarnation": "incarnation-9",
        "source_evidence_identity": "cycle-1",
        "invocation_input": "repair this exact PR operation",
    }
    values.update(changes)
    return LocalPRRepairOffer(**values)  # type: ignore[arg-type]


def client() -> MagicMock:
    github = MagicMock()
    github.get_pull_request_metadata_strict.return_value = {
        "state": "open",
        "merged_at": None,
        "head": {"ref": "feature", "sha": "H1", "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main", "sha": "B1", "repo": {"full_name": "owner/repo"}},
    }
    return github


def offer(store: LocalJobStore, item: LocalPRRepairOffer | None = None, github: MagicMock | None = None):
    item = item or request()
    observed = snapshot()
    authority = CIRepairAuthority(True, "failure", "H1", observed)
    return offer_local_pr_repair_job(item, github or client(), owner_admission=lambda _: True, ci_authority=authority, store=store)


def test_racing_controllers_replay_one_durable_exact_head_envelope(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    stores = (LocalJobStore(path), LocalJobStore(path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(offer, stores))

    assert results[0] is not None
    assert results[1] is not None
    assert results[0].job_id == results[1].job_id
    assert len(LocalJobStore(path).discover_unsettled()) == 1
    assert LocalJobStore(path).claim(results[0].job_id).acquired is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda github: github.get_pull_request_metadata_strict.return_value["head"].update(sha="H2"),
        lambda github: github.get_pull_request_metadata_strict.return_value["base"].update(sha="B2"),
        lambda github: github.get_pull_request_metadata_strict.return_value["head"]["repo"].update(full_name="fork/repo"),
    ],
)
def test_changed_head_base_and_wrong_origin_fail_closed(tmp_path: Path, mutate) -> None:
    github = client()
    mutate(github)
    assert offer(LocalJobStore(tmp_path / "jobs.sqlite3"), github=github) is None


@pytest.mark.parametrize("conclusion", [CIConclusion.SUCCESS, CIConclusion.PENDING])
def test_ci_without_complete_terminal_failure_is_not_admitted(tmp_path: Path, conclusion: CIConclusion) -> None:
    authority = CIRepairAuthority(True, "not authoritative", "H1", snapshot(conclusion))
    assert offer_local_pr_repair_job(request(), client(), owner_admission=lambda _: True, ci_authority=authority, store=LocalJobStore(tmp_path / "jobs.sqlite3")) is None


def test_known_empty_ci_does_not_grant_ci_failure_admission(tmp_path: Path) -> None:
    observed = snapshot(availability=ObservationAvailability.KNOWN_EMPTY)
    # Deliberately pass a contradictory ``allowed`` bit to prove the producer
    # re-evaluates the complete snapshot rather than flattening availability.
    authority = CIRepairAuthority(True, "incorrect aggregate", "H1", observed)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")

    assert offer_local_pr_repair_job(request(), client(), owner_admission=lambda _: True, ci_authority=authority, store=store) is None
    assert store.discover_unsettled() == ()


def test_known_empty_ci_does_not_block_affirmative_conflict(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    accepted = offer_local_pr_repair_job(
        request(RepairKind.MERGE_CONFLICT),
        client(),
        owner_admission=lambda _: True,
        conflict_eligible=True,
        store=store,
    )
    assert accepted is not None
    assert accepted.kind is LocalJobKind.PR_MERGE_CONFLICT


def test_competing_review_correction_suppresses_branch_edit_job(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    competing = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 17, "review-attempt", "codex", "review")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._insert(connection, competing, "review-incarnation")
        connection.commit()
    assert offer(store) is None


def test_same_controller_progresses_once_after_review_blocker_settles(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    competing = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 17, "review-attempt", "codex", "review")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._insert(connection, competing, "review-incarnation")
        connection.commit()

    assert offer(store) is None
    review_claim = store.claim(competing.job_id, "same-controller")
    assert review_claim is not None and review_claim.acquired
    artifact = store.persist_result_artifact(review_claim, InvocationOutcome.FAILED, "temporary blocker removed")
    assert artifact is not None
    assert store.record_result(review_claim, InvocationOutcome.FAILED, artifact.artifact_id)
    assert store.mark_downstream_pending(review_claim)
    assert store.settle(review_claim)

    accepted = offer(store)
    replay = offer(store)
    assert accepted is not None and replay is not None
    assert accepted.job_id == replay.job_id
    repair_jobs = tuple(job for job in store.discover_unsettled() if job.kind is LocalJobKind.PR_CI_FAILURE)
    assert [job.job_id for job in repair_jobs] == [accepted.job_id]
    repair_claim = store.claim(accepted.job_id, "same-controller")
    assert repair_claim is not None and repair_claim.acquired


def test_failed_persistence_returns_no_acceptance(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x")
    store = object.__new__(LocalJobStore)
    store.path = blocked / "jobs.sqlite3"
    assert offer(store) is None
