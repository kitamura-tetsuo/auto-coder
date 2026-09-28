from pathlib import Path

import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_observation import ObservationBinding
from auto_coder.codex_pr_attribution import CodexPrAttributionRepository
from auto_coder.codex_retirement_observer import CodexEvidenceState, _task_state, collect_codex_retirement_observation
from auto_coder.codex_wham_client import WhamTask, WhamTurn
from auto_coder.codex_work_accounting import CodexWorkAccounting, CodexWorkPhase
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

OWNER = ImplementationOwner("issue", 2335)
REPOSITORY = "owner/repo"
TASK = "task_e_1234567890abcdef"
TASK_2 = "task_e_abcdef1234567890"


class WhamReader:
    def __init__(self, task: WhamTask, turns: list[WhamTurn]) -> None:
        self.task = task
        self.turns = turns

    def get_task(self, task_id: str) -> WhamTask:
        assert task_id == TASK
        return self.task

    def get_task_turns(self, task_id: str) -> list[WhamTurn]:
        assert task_id == TASK
        return self.turns


class MultiWhamReader:
    def __init__(self, tasks: dict[str, WhamTask], turns: dict[str, list[WhamTurn]]) -> None:
        self.tasks = tasks
        self.turns = turns

    def get_task(self, task_id: str) -> WhamTask:
        return self.tasks[task_id]

    def get_task_turns(self, task_id: str) -> list[WhamTurn]:
        return self.turns[task_id]


class GitHubReader:
    def __init__(self, prs: dict[int, dict[str, object]]) -> None:
        self.prs = prs

    def get_connected_prs(self, repository: str, issue: int, strict: bool = False) -> list[int]:
        assert (repository, issue, strict) == (REPOSITORY, 2335, True)
        return [11]

    def get_open_pull_requests_strict(self, repository: str) -> list[dict[str, object]]:
        assert repository == REPOSITORY
        return [self.prs[12]]

    def get_pull_request_metadata_strict(self, repository: str, number: int) -> dict[str, object]:
        assert repository == REPOSITORY
        return self.prs[number]


def _stores(tmp_path: Path) -> tuple[ImplementationSlotRepository, CloudRunRepository, str]:
    slots = ImplementationSlotRepository(REPOSITORY, 2, storage_path=tmp_path / "slots.json")
    assert slots.reserve(OWNER)
    incarnation = slots.owner_incarnation(OWNER)
    assert incarnation is not None
    runs = CloudRunRepository(REPOSITORY, storage_path=tmp_path / "runs.json")
    runs.save(
        CloudRun(
            REPOSITORY,
            2335,
            0,
            "codex-cloud",
            TASK,
            "codex-cloud",
            "environment-1",
            "main",
            pull_request_numbers=[10],
            publication_head_repository=REPOSITORY,
            publication_head_ref="issue-2335-attempt-0-codex-cloud",
        )
    )
    return slots, runs, incarnation


def _terminal_task() -> tuple[WhamTask, list[WhamTurn]]:
    baseline = WhamTurn(f"{TASK}~assttrn_baseline", "assistant", "completed")
    request = WhamTurn("user-turn", "user", "completed", raw_data={"request_id": "repair-request"})
    terminal = WhamTurn(f"{TASK}~assttrn_terminal", "assistant", "completed")
    task = WhamTask(
        TASK,
        turns=[baseline, request, terminal],
        current_user_turn=WhamTurn("user-turn", "user", "completed"),
        current_assistant_turn=terminal,
        latest_turn_status="completed",
        environment_id="environment-1",
    )
    return task, [baseline, request, terminal]


def _current_task(task_id: str, status: str, *, latest: str | None = None, user_status: str = "completed", environment: str = "environment-1") -> WhamTask:
    assistant = WhamTurn(f"{task_id}~assttrn_current", "assistant", status)
    return WhamTask(
        task_id,
        turns=[assistant],
        current_user_turn=WhamTurn(f"{task_id}~user_current", "user", user_status),
        current_assistant_turn=assistant,
        latest_turn_status=latest if latest is not None else status,
        environment_id=environment,
    )


def _settled_initial_snapshot(slots: ImplementationSlotRepository, incarnation: str):
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="initial",
        kind="submission",
        source_request_id="creation-request",
        task_id=TASK,
    )
    return accounting.transition(
        OWNER,
        incarnation,
        "initial",
        CodexWorkPhase.SETTLED,
        evidence_id="terminal-initial",
        evidence_source_request_id="creation-request",
        task_id=TASK,
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )


def test_collects_all_durable_and_discovered_prs_without_writes(tmp_path: Path) -> None:
    slots, runs, incarnation = _stores(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    assert slots.reserve(OWNER, implementation_pr=10)
    registration = accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="repair",
        kind="repair",
        source_request_id="repair-request",
        causal_baseline=f"{TASK}~assttrn_baseline",
        task_id=TASK,
    )
    snapshot = accounting.transition(
        OWNER,
        incarnation,
        "repair",
        CodexWorkPhase.SETTLED,
        evidence_id="settlement",
        evidence_causal_baseline=f"{TASK}~assttrn_baseline",
        evidence_source_request_id="repair-request",
        task_id=TASK,
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )
    assert registration.created
    prs = {
        10: {"number": 10, "state": "closed"},
        11: {"number": 11, "state": "closed"},
        12: {
            "number": 12,
            "state": "open",
            "body": f"Closes #2335 https://chatgpt.com/codex/tasks/{TASK}",
            "head": {"ref": "issue-2335-attempt-0-codex-cloud"},
        },
    }
    task, turns = _terminal_task()
    attribution_path = tmp_path / "attributions.json"
    before_slots = (tmp_path / "slots.json").read_bytes()
    before_runs = (tmp_path / "runs.json").read_bytes()

    result = collect_codex_retirement_observation(
        REPOSITORY,
        OWNER,
        incarnation,
        slots,
        snapshot,
        runs,
        GitHubReader(prs),  # type: ignore[arg-type]
        wham=WhamReader(task, turns),  # type: ignore[arg-type]
        attributions=CodexPrAttributionRepository(REPOSITORY, attribution_path),
    )

    assert [item.number for item in result.implementation_prs] == [10, 11, 12]
    assert result.task_evidence[0].state is CodexEvidenceState.TERMINAL
    assert result.settlement_certificates[0].logical_operation_id == "repair"
    assert result.conclusive is False
    assert before_slots == (tmp_path / "slots.json").read_bytes()
    assert before_runs == (tmp_path / "runs.json").read_bytes()
    assert not attribution_path.exists()


def test_old_completion_cannot_settle_unmatched_new_admission(tmp_path: Path) -> None:
    slots, runs, incarnation = _stores(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    assert slots.reserve(OWNER, implementation_pr=10)
    snapshot = accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="new-repair",
        kind="repair",
        source_request_id="missing-request",
        causal_baseline=f"{TASK}~assttrn_baseline",
        task_id=TASK,
    ).snapshot
    task, turns = _terminal_task()
    prs = {
        10: {"number": 10, "state": "closed"},
        11: {"number": 11, "state": "closed"},
        12: {"number": 12, "state": "closed", "body": "unrelated"},
    }

    result = collect_codex_retirement_observation(
        REPOSITORY,
        OWNER,
        incarnation,
        slots,
        snapshot,
        runs,
        GitHubReader(prs),  # type: ignore[arg-type]
        wham=WhamReader(task, turns),  # type: ignore[arg-type]
        attributions=CodexPrAttributionRepository(REPOSITORY, tmp_path / "attributions.json"),
    )

    assert result.settlement_certificates == ()
    assert "operation new-repair lacks causal settlement evidence" in result.incomplete_reasons
    assert result.conclusive is False


def test_stored_settlement_without_matching_turn_history_remains_incomplete(tmp_path: Path) -> None:
    slots, runs, incarnation = _stores(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    assert slots.reserve(OWNER, implementation_pr=10)
    accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="repair",
        kind="repair",
        source_request_id="missing-request",
        causal_baseline=f"{TASK}~assttrn_baseline",
        task_id=TASK,
    )
    snapshot = accounting.transition(
        OWNER,
        incarnation,
        "repair",
        CodexWorkPhase.SETTLED,
        evidence_id="stored-claim",
        evidence_causal_baseline=f"{TASK}~assttrn_baseline",
        evidence_source_request_id="missing-request",
        task_id=TASK,
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )
    task, turns = _terminal_task()
    prs = {
        10: {"number": 10, "state": "closed"},
        11: {"number": 11, "state": "closed"},
        12: {"number": 12, "state": "closed", "body": "unrelated"},
    }

    result = collect_codex_retirement_observation(
        REPOSITORY,
        OWNER,
        incarnation,
        slots,
        snapshot,
        runs,
        GitHubReader(prs),  # type: ignore[arg-type]
        wham=WhamReader(task, turns),  # type: ignore[arg-type]
        attributions=CodexPrAttributionRepository(REPOSITORY, tmp_path / "attributions.json"),
    )

    assert result.settlement_certificates == ()
    assert "operation repair lacks causal settlement evidence" in result.incomplete_reasons
    assert result.conclusive is False


def test_all_retained_attempts_are_observed_and_active_earlier_work_blocks(tmp_path: Path) -> None:
    slots, runs, incarnation = _stores(tmp_path)
    _settled_initial_snapshot(slots, incarnation)
    assert slots.reserve(OWNER, implementation_pr=10)
    snapshot = CodexWorkAccounting(slots).snapshot(OWNER, incarnation)
    runs.save(
        CloudRun(
            REPOSITORY,
            2335,
            1,
            "codex-cloud",
            TASK_2,
            "codex-cloud",
            "environment-1",
            "main",
        )
    )
    completed = _current_task(TASK, "completed")
    running = _current_task(TASK_2, "in_progress")
    prs = {
        10: {"number": 10, "state": "closed"},
        11: {"number": 11, "state": "closed"},
        12: {"number": 12, "state": "closed", "body": "unrelated"},
    }

    result = collect_codex_retirement_observation(
        REPOSITORY,
        OWNER,
        incarnation,
        slots,
        snapshot,
        runs,
        GitHubReader(prs),  # type: ignore[arg-type]
        wham=MultiWhamReader({TASK: completed, TASK_2: running}, {TASK: completed.turns, TASK_2: running.turns}),  # type: ignore[arg-type]
        attributions=CodexPrAttributionRepository(REPOSITORY, tmp_path / "attributions.json"),
    )

    assert [(item.task_id, item.attempt, item.state) for item in result.task_evidence] == [
        (TASK, 0, CodexEvidenceState.TERMINAL),
        (TASK_2, 1, CodexEvidenceState.ACTIVE_RUNNING),
    ]
    assert [certificate.logical_operation_id for certificate in result.settlement_certificates] == ["initial"]
    assert result.conclusive is False


class ChangingRunInventory:
    def __init__(self, runs: CloudRunRepository) -> None:
        self.runs = runs
        self.reads = 0

    def list_for_issue(self, issue_number: int) -> list[CloudRun]:
        values = self.runs.list_for_issue(issue_number)
        self.reads += 1
        if self.reads == 1:
            return values
        changed = CloudRun.from_dict(values[0].to_dict())
        changed.pull_request_numbers.append(99)
        return [changed]


@pytest.mark.parametrize("changing", [False, True], ids=["stable", "changed"])
def test_cloudrun_provenance_revalidation_fences_changed_inventory(tmp_path: Path, changing: bool) -> None:
    slots, runs, incarnation = _stores(tmp_path)
    _settled_initial_snapshot(slots, incarnation)
    assert slots.reserve(OWNER, implementation_pr=10)
    snapshot = CodexWorkAccounting(slots).snapshot(OWNER, incarnation)
    completed = _current_task(TASK, "completed")
    prs = {
        10: {"number": 10, "state": "closed"},
        11: {"number": 11, "state": "closed"},
        12: {"number": 12, "state": "closed", "body": "unrelated"},
    }
    run_source = ChangingRunInventory(runs) if changing else runs

    result = collect_codex_retirement_observation(
        REPOSITORY,
        OWNER,
        incarnation,
        slots,
        snapshot,
        run_source,  # type: ignore[arg-type]
        GitHubReader(prs),  # type: ignore[arg-type]
        wham=WhamReader(completed, completed.turns),  # type: ignore[arg-type]
        attributions=CodexPrAttributionRepository(REPOSITORY, tmp_path / "attributions.json"),
    )

    provenance = {item.source: item.consistency_identity for item in result.provenance}
    assert provenance["cloud-runs-and-publication-inventory"]
    assert provenance["work-accounting"]
    if changing:
        assert "CloudRun/publication inventory changed during collection" in result.incomplete_reasons
        assert result.conclusive is False
    else:
        assert result.incomplete_reasons == ()
        assert result.conclusive is True


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (_current_task(TASK, "queued"), CodexEvidenceState.ACTIVE_QUEUED),
        (_current_task(TASK, "in_progress"), CodexEvidenceState.ACTIVE_RUNNING),
        (_current_task(TASK, "paused"), CodexEvidenceState.ACTIVE_PAUSED),
        (_current_task(TASK, "waiting_for_input"), CodexEvidenceState.ACTIVE_PAUSED),
        (_current_task(TASK, "failed"), CodexEvidenceState.TERMINAL_FAILED),
        (_current_task(TASK, "error"), CodexEvidenceState.TERMINAL_FAILED),
        (_current_task(TASK, "cancelled"), CodexEvidenceState.TERMINAL_CANCELLED),
        (_current_task(TASK, "canceled"), CodexEvidenceState.TERMINAL_CANCELLED),
        (_current_task(TASK, "completed", latest="in_progress"), CodexEvidenceState.UNKNOWN),
        (_current_task(TASK, "completed", user_status="pending"), CodexEvidenceState.UNKNOWN),
        (_current_task(TASK, "completed", environment="wrong-environment"), CodexEvidenceState.UNKNOWN),
        (
            WhamTask(
                TASK,
                current_assistant_turn=WhamTurn("foreign-assistant", "assistant", "completed"),
                latest_turn_status="completed",
                environment_id="environment-1",
            ),
            CodexEvidenceState.UNKNOWN,
        ),
        (_current_task("task_e_foreign123456789", "completed"), CodexEvidenceState.UNKNOWN),
        (WhamTask(TASK, latest_turn_status="completed", environment_id="environment-1"), CodexEvidenceState.UNKNOWN),
        (None, CodexEvidenceState.UNKNOWN),
    ],
)
def test_current_wham_evidence_is_normalized_fail_closed(task: WhamTask | None, expected: CodexEvidenceState) -> None:
    binding = ObservationBinding(REPOSITORY, 2335, 0, "codex-cloud", TASK, "codex-cloud", "environment-1", "main")

    evidence = _task_state(task, binding)

    assert evidence.state is expected
