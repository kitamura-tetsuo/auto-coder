from types import SimpleNamespace

from auto_coder.ci_observation import ObservationAvailability, WorkflowObservation
from auto_coder.github_ci_observer import approve_waiting_deployment, ci_read_phase, observe_ci


class Actions:
    def __init__(self):
        self.reads = 0
        self.reviews = 0

    def list_workflow_runs_for_repo(self, owner, repo, **kwargs):
        self.reads += 1
        return {
            "workflow_runs": [
                {
                    "id": 20,
                    "workflow_id": 7,
                    "run_attempt": 2,
                    "head_sha": "head",
                    "status": "completed",
                    "conclusion": "success",
                    "name": "CI",
                    "executed_test_targets": ["tests/test_feature.py::test_case"],
                }
            ]
        }

    def get_workflow_run(self, owner, repo, run_id):
        return {"id": run_id, "run_attempt": 2, "head_sha": "head", "status": "waiting"}

    def get_pending_deployments_for_run(self, owner, repo, run_id):
        return [{"environment": {"id": 3}}]

    def review_pending_deployments_for_run(self, owner, repo, run_id, **kwargs):
        self.reviews += 1


class Checks:
    def __init__(self):
        self.reads = 0

    def list_for_ref(self, owner, repo, **kwargs):
        self.reads += 1
        return {"check_runs": [{"id": 9, "app": {"id": 4}, "head_sha": "head", "status": "completed", "conclusion": "success", "name": "CI"}]}


def test_phase_reuses_real_targeted_adapter_reads_and_preserves_identity():
    api = SimpleNamespace(actions=Actions(), checks=Checks())
    with ci_read_phase("selection"):
        first = observe_ci(api, "credential", "owner/repo", 12, "head")
        second = observe_ci(api, "credential", "owner/repo", 12, "head")
    assert first is second
    assert first.availability is ObservationAvailability.KNOWN
    assert api.actions.reads == 1
    assert api.checks.reads == 1
    workflow = next(f for f in first.facts if isinstance(f, WorkflowObservation))
    assert (workflow.execution.workflow_id, workflow.execution.run_id, workflow.execution.attempt) == ("7", "20", 2)
    assert workflow.successful_test_targets == ("tests/test_feature.py::test_case",)


def test_partial_source_never_becomes_complete_or_cached_success():
    api = SimpleNamespace(actions=Actions(), checks=Checks())
    api.actions.list_workflow_runs_for_repo = lambda *args, **kwargs: {"wrong": []}
    snapshot = observe_ci(api, "other-credential", "owner/repo", 12, "head")
    assert snapshot.availability is ObservationAvailability.PARTIAL
    assert not snapshot.complete
    assert snapshot.facts == ()


def test_approval_is_explicit_fresh_and_confirmed_delivery_is_not_repeated():
    api = SimpleNamespace(actions=Actions(), checks=Checks())
    assert approve_waiting_deployment(api, "approval-credential", "owner/repo", 20, 2, "head") is True
    assert approve_waiting_deployment(api, "approval-credential", "owner/repo", 20, 2, "head") is True
    assert api.actions.reviews == 1
