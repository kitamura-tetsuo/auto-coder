import inspect
from dataclasses import dataclass, field

from auto_coder import pr_processor, pr_review_resumption
from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.pr_review_resumption import RetainedClosureResumption, SavedReviewRequest, SavedReviewResumptionService


class Actions(list[str]):
    quota_deferred = False
    retry_not_before = None


@dataclass
class Ports:
    events: list[str] = field(default_factory=list)
    closure: RetainedClosureResumption = field(default_factory=RetainedClosureResumption)

    def resolve_two_tier(self, github_client, repository, pr_data):
        self.events.append("identity")
        return object()

    def resume_retained_closure(self, repository, pr_number, head_sha, inputs, github_client):
        self.events.append("closure")
        return self.closure

    def repair_strong_findings(self, request, inputs):
        self.events.append("strong")
        return False

    def record_identity_deferral(self, pr_number, head_sha, reason):
        self.events.append("identity-deferral")

    def record_closure_deferral(self, pr_number, head_sha, reason):
        self.events.append("closure-deferral")


def request(actions, status=None, *, force=False):
    return SavedReviewRequest(object(), "owner/repo", {"number": 7}, "head", force, AutomationConfig(), actions, status)


def test_pre_admission_resumption_preserves_phase_order():
    ports = Ports(closure=RetainedClosureResumption(ordinary_pass_retained=True))
    result = SavedReviewResumptionService(ports).resume_before_admission(request(Actions()))

    assert not result.stop_processing
    assert ports.events == ["identity", "closure"]


def test_unavailable_closure_defers_for_sixty_seconds_without_admitting_work():
    ports = Ports(closure=RetainedClosureResumption(unavailable=True, reason="store unavailable"))
    actions = Actions()
    status = ProcessedPRResult(pr_data={"number": 7})

    result = SavedReviewResumptionService(ports, clock=lambda: 100.0).resume_before_admission(request(actions, status))

    assert result.stop_processing
    assert actions.quota_deferred
    assert actions.retry_not_before == 160.0
    assert status.outcome is PRProcessingOutcome.DEFERRED
    assert status.retry_not_before == 160.0
    assert ports.events == ["identity", "closure", "closure-deferral"]


def test_unavailable_discussion_defers_even_when_forced():
    ports = Ports(closure=RetainedClosureResumption(unavailable=True, discussion_unavailable=True, reason="discussion unavailable"))
    actions = Actions()
    result = SavedReviewResumptionService(ports, clock=lambda: 100.0).resume_before_admission(request(actions, force=True))
    assert result.stop_processing
    assert ports.events == ["identity", "closure", "closure-deferral"]
    assert actions.retry_not_before == 160.0


def test_saved_decision_policy_is_implemented_in_the_extracted_module():
    extracted = inspect.getsource(pr_review_resumption)
    processor = inspect.getsource(pr_processor)

    for implementation in (
        "def _resume_saved_ordinary_correction(",
        "def _saved_repair_has_current_evidence(",
        "def assess_saved_review_state(",
        "def replay_saved_nonpass_review(",
    ):
        assert implementation in extracted
    assert "def _assess_saved_review_state_impl(" not in processor
    assert "def _replay_saved_nonpass_review_impl(" not in processor
    assert "def _resume_saved_ordinary_correction(" not in processor
