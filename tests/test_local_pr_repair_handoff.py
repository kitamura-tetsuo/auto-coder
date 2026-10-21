from pathlib import Path
from unittest.mock import Mock, patch

from auto_coder import pr_processor
from auto_coder.local_job_handoff import LocalJobKind, LocalJobStore
from auto_coder.pr_processor import LocalPRRepairContext, local_pr_repair_context
from auto_coder.pr_repair_dispatch import RepairDisposition, RepairKind


def metadata() -> dict:
    return {
        "number": 17,
        "state": "open",
        "merged": False,
        "body": "",
        "user": {"login": "human"},
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": "H1"},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": "B1"},
    }


def test_typed_handoff_binds_exact_implementation_execution(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = metadata()
    context = LocalPRRepairContext(store, "execution-17")

    with (
        local_pr_repair_context(context),
        patch.object(pr_processor, "_selected_local_backend_name", return_value="codex"),
        patch.object(pr_processor, "check_pr_repair_exhaustion", return_value=None),
    ):
        result = pr_processor._offer_selected_local_pr_repair("owner/repo", metadata(), RepairKind.MERGE_CONFLICT, github, conflict_eligible=True)

    assert result is not None
    assert result.disposition is RepairDisposition.DEFERRED
    assert result.local_handoff is not None
    assert result.local_handoff.implementation_execution_id == "execution-17"
    retained = store.pr_repair_for_execution("owner/repo", 17, "execution-17")
    assert retained is not None
    assert retained.job_id == result.local_handoff.job_id
    assert retained.kind is LocalJobKind.PR_MERGE_CONFLICT


def test_public_offer_without_engine_execution_context_does_not_admit(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = metadata()
    with patch.object(pr_processor, "_selected_local_backend_name", return_value="codex"):
        result = pr_processor._offer_selected_local_pr_repair("owner/repo", metadata(), RepairKind.MERGE_CONFLICT, github, conflict_eligible=True, store=store)
    assert result is None
    assert store.discover_unsettled() == ()
