"""First-use local PR repair admission with real configuration and SQLite stores."""

from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder import pr_processor
from auto_coder.canonical_pr_blocker_ledger import BlockerLedgerUnavailableError, BlockerLedgerUninitializedError, CanonicalPRBlockerLedger
from auto_coder.durable_repair_allowance import RepairAllowanceLedger, RepairAllowanceUnavailableError, RepairAllowanceUninitializedError
from auto_coder.execution_trace import Outcome, get_trace_collector
from auto_coder.llm_backend_config import LLMBackendConfiguration, get_llm_config, resolve_repo_override_path
from auto_coder.local_job_handoff import LocalJobKind, LocalJobState, LocalJobStore
from auto_coder.pr_repair_dispatch import RepairDisposition, RepairKind
from auto_coder.pr_repair_guard import check_pr_repair_exhaustion
from tests.test_dashboard_observability import _assert_required_stage_visible, _mounted_detail
from tests.test_local_pr_repair_job import client, snapshot
from tests.test_pr_repair_exhaustion import PR_NUMBER, REPO, _exhaust_blocker

pytestmark = pytest.mark.pr_repair_contract


@pytest.fixture
def repair_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "llm_config.toml"
    path.write_text(
        '[backend]\norder = ["codex-cloud"]\n' '[backends.codex-cloud]\nbackend_type = "codex-cloud"\n' '[backend_for_failed_pr]\norder = ["local-edit"]\n' '[backends.local-edit]\nbackend_type = "codex"\nmodel = "repair-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_CODER_LLM_CONFIG_PATH", str(path))
    monkeypatch.delenv("AUTO_CODER_CONFIG_PATH", raising=False)
    return path


@pytest.mark.parametrize("initialize_blockers", [False, True])
@pytest.mark.parametrize("initialize_allowance", [False, True])
def test_strict_first_use_guard_accepts_readable_uninitialized_namespaces(tmp_path: Path, initialize_blockers: bool, initialize_allowance: bool) -> None:
    blockers = CanonicalPRBlockerLedger(tmp_path / "blockers.db")
    allowance = RepairAllowanceLedger(tmp_path / "allowance.db")
    if initialize_blockers:
        blockers.initialize_namespace("https://api.github.com", "owner/repo", 17)
    if initialize_allowance:
        allowance.initialize_namespace("https://api.github.com", "owner/repo", 17)

    assert check_pr_repair_exhaustion("owner/repo", 17, blocker_ledger=blockers, allowance_ledger=allowance, require_authoritative_snapshot=True) is None

    # Inspection must not create retained namespaces or overwrite first-use state.
    if not initialize_blockers:
        with pytest.raises(BlockerLedgerUninitializedError, match="is uninitialized"):
            blockers.get_snapshot("https://api.github.com", "owner/repo", 17, require_retained_state=False)
        with pytest.raises(BlockerLedgerUnavailableError, match="Required retained state is missing"):
            blockers.get_snapshot("https://api.github.com", "owner/repo", 17)
    if not initialize_allowance:
        with pytest.raises(RepairAllowanceUninitializedError, match="is uninitialized"):
            allowance.get_snapshot("https://api.github.com", "owner/repo", 17, require_retained_state=False)
        with pytest.raises(RepairAllowanceUnavailableError, match="Required retained state is missing"):
            allowance.get_snapshot("https://api.github.com", "owner/repo", 17)


@pytest.mark.parametrize("ledger_kind", ["blockers", "allowance"])
def test_strict_guard_rejects_corrupt_storage(tmp_path: Path, ledger_kind: str) -> None:
    blockers = CanonicalPRBlockerLedger(tmp_path / "blockers.db")
    allowance = RepairAllowanceLedger(tmp_path / "allowance.db")
    (tmp_path / f"{ledger_kind}.db").write_bytes(b"not a SQLite database")
    expected = BlockerLedgerUnavailableError if ledger_kind == "blockers" else RepairAllowanceUnavailableError
    with pytest.raises(expected):
        check_pr_repair_exhaustion("owner/repo", 17, blocker_ledger=blockers, allowance_ledger=allowance, require_authoritative_snapshot=True)


def test_strict_guard_preserves_exhausted_allowance_without_canonical_namespace(tmp_path: Path) -> None:
    blockers = CanonicalPRBlockerLedger(tmp_path / "blockers.db")
    allowance = RepairAllowanceLedger(tmp_path / "allowance.db")
    _exhaust_blocker(allowance, "ci-failure", limit=1)
    result = check_pr_repair_exhaustion(REPO, PR_NUMBER, blocker_ledger=blockers, allowance_ledger=allowance, require_authoritative_snapshot=True)
    assert result is not None and result.is_exhausted is True
    assert result.exhausted_blocker_ids == ("ci-failure",)
    assert result.blockers[0].total_failed_count == 1
    assert result.machine_readable_reason == "AUTO_REPAIR_EXHAUSTED"


@pytest.mark.parametrize("kind", [RepairKind.CI_FAILURE, RepairKind.MERGE_CONFLICT])
@patch("auto_coder.dashboard.ui")
def test_first_local_repair_admission_replays_and_reaches_mounted_detail(mock_ui, tmp_path: Path, repair_policy: Path, kind: RepairKind) -> None:
    from auto_coder.ci_repair_authority import CIRepairAuthority

    github = client()
    metadata = github.get_pull_request_metadata_strict.return_value | {"number": 17}
    path = tmp_path / "jobs.sqlite3"
    collector = get_trace_collector()
    collector.clear()
    authority = CIRepairAuthority(True, "complete terminal failure", "H1", snapshot())
    with (
        patch.object(pr_processor, "get_llm_backend_manager", side_effect=AssertionError("admission must not initialize a client")) as manager,
        collector.start_execution("owner/repo", "pr", 17, origin="worker"),
    ):
        results = []
        for _ in range(2):
            # Reconstruct the store to cover a repeated controller observation.
            with pr_processor.local_pr_repair_context(pr_processor.LocalPRRepairContext(LocalJobStore(path), "execution-17")):
                results.append(pr_processor._offer_selected_local_pr_repair("owner/repo", metadata, kind, github, ci_authority=authority, conflict_eligible=True))
        manager.assert_not_called()

    first, replay = results
    assert first is not None and replay is not None
    assert first.disposition is replay.disposition is RepairDisposition.DEFERRED
    assert first.receipt == replay.receipt
    assert first.local_handoff is not None
    assert first.local_handoff.implementation_execution_id == "execution-17"
    jobs = LocalJobStore(path).discover_unsettled()
    assert len(jobs) == 1
    assert jobs[0].backend_name == "local-edit"
    assert jobs[0].kind is (LocalJobKind.PR_CI_FAILURE if kind is RepairKind.CI_FAILURE else LocalJobKind.PR_MERGE_CONFLICT)
    assert jobs[0].state is LocalJobState.PENDING
    assert jobs[0].provider_entered is False
    events = [event for event in collector.get_snapshot(repository="owner/repo", item_type="pr", item_number=17).events if event.stage_id == "pr.repair-delegation"]
    assert len(events) == 2
    assert all(event.outcome == Outcome.DEFERRED.value for event in events)
    assert all(event.facts["admission_status"] == "admitted" and event.facts["backend"] == "local-edit" and event.facts["job_id"] == first.receipt for event in events)
    _assert_required_stage_visible(_mounted_detail(mock_ui, "pr", 17), "local repair admission")
    with pytest.raises(BlockerLedgerUninitializedError):
        CanonicalPRBlockerLedger().get_snapshot("https://api.github.com", "owner/repo", 17, require_retained_state=False)
    with pytest.raises(RepairAllowanceUninitializedError):
        RepairAllowanceLedger().get_snapshot("https://api.github.com", "owner/repo", 17, require_retained_state=False)


@pytest.mark.parametrize("blocked_by", ["blockers", "allowance", "no-backend"])
@patch("auto_coder.dashboard.ui")
def test_refused_admission_preserves_reason_in_mounted_detail(mock_ui, tmp_path: Path, repair_policy: Path, blocked_by: str) -> None:
    from auto_coder.ci_repair_authority import CIRepairAuthority

    if blocked_by == "blockers":
        ledger = CanonicalPRBlockerLedger()
        ledger._db_path.parent.mkdir(parents=True, exist_ok=True)
        ledger._db_path.write_bytes(b"not a SQLite database")
        expected_reason = "BlockerLedgerUnavailableError"
    elif blocked_by == "allowance":
        allowance = RepairAllowanceLedger()
        allowance._db_path.parent.mkdir(parents=True, exist_ok=True)
        allowance._db_path.write_bytes(b"not a SQLite database")
        expected_reason = "RepairAllowanceUnavailableError"
    else:
        repair_policy.write_text(repair_policy.read_text(encoding="utf-8").replace('order = ["local-edit"]', "order = []"), encoding="utf-8")
        expected_reason = "No local PR repair backend is configured"
    github = client()
    metadata = github.get_pull_request_metadata_strict.return_value | {"number": 17}
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    collector = get_trace_collector()
    collector.clear()
    with collector.start_execution("owner/repo", "pr", 17, origin="worker"), pr_processor.local_pr_repair_context(pr_processor.LocalPRRepairContext(jobs, "execution-17")):
        result = pr_processor._offer_selected_local_pr_repair("owner/repo", metadata, RepairKind.CI_FAILURE, github, ci_authority=CIRepairAuthority(True, "failure", "H1", snapshot()))

    assert result is not None and result.disposition is RepairDisposition.DEFERRED
    assert result.receipt == "" and result.local_handoff is None
    assert expected_reason in result.reason
    assert jobs.discover_unsettled() == ()
    events = [event for event in collector.get_snapshot(repository="owner/repo", item_type="pr", item_number=17).events if event.stage_id == "pr.repair-delegation"]
    assert len(events) == 1
    assert events[0].outcome == Outcome.DEFERRED.value
    assert events[0].facts["admission_status"] == "deferred"
    assert events[0].facts["job_id"] == ""
    assert expected_reason in events[0].facts["reason"]
    _assert_required_stage_visible(_mounted_detail(mock_ui, "pr", 17), "local repair admission")


@pytest.mark.parametrize("order", [["local-edit", "codex"], []])
def test_local_repair_policy_round_trips_and_overrides_per_repository(tmp_path: Path, repair_policy: Path, order: list[str]) -> None:
    config = LLMBackendConfiguration.load_from_file(str(repair_policy))
    config.backend_for_failed_pr_order = order
    saved = tmp_path / "saved.toml"
    config.save_to_file(str(saved))
    loaded = LLMBackendConfiguration.load_from_file(str(saved))
    assert loaded.backend_for_failed_pr_explicit is True
    assert loaded.get_local_pr_repair_backends() == order
    override_path = resolve_repo_override_path("owner/repo")
    assert override_path is not None
    override = Path(override_path)
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text('[backend_for_failed_pr]\norder = ["codex"]\n', encoding="utf-8")
    assert get_llm_config(repo_name="owner/repo").get_local_pr_repair_backends() == ["codex"]
    assert get_llm_config(repo_name="owner/other").get_local_pr_repair_backends() == ["local-edit"]


@pytest.mark.parametrize("ordinary,expected", [(["jules", "codex"], ["codex"]), (["codex-cloud"], []), ([], [])])
def test_absent_repair_policy_only_uses_local_ordinary_candidates(ordinary: list[str], expected: list[str]) -> None:
    config = LLMBackendConfiguration.load_from_dict({"backend": {"order": ordinary}, "backends": {"codex-cloud": {"backend_type": "codex-cloud"}}})
    assert config.get_local_pr_repair_backends() == expected


def test_disabled_repair_candidate_does_not_select_unlisted_default() -> None:
    config = LLMBackendConfiguration.load_from_dict({"backend_for_failed_pr": {"order": ["codex"]}, "backends": {"codex": {"enabled": False}}})
    assert config.get_local_pr_repair_backends() == []


@pytest.mark.parametrize("section", [[], {}, {"order": "codex"}, {"order": [""]}, {"order": ["unknown"]}, {"order": ["jules"]}, {"order": ["cloud-alias"]}, {"priority_groups": [["codex"]]}])
def test_invalid_local_repair_policy_is_rejected(section) -> None:
    with pytest.raises(ValueError, match="backend_for_failed_pr"):
        LLMBackendConfiguration.load_from_dict({"backend_for_failed_pr": section, "backends": {"cloud-alias": {"backend_type": "codex-cloud"}}})
