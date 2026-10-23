from dataclasses import dataclass, field

from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.pr_review_resumption import RetainedClosureResumption, SavedReviewRequest, SavedReviewResumptionService


class Actions(list[str]):
    quota_deferred = False
    retry_not_before = None


@dataclass
class Ports:
    events: list[str] = field(default_factory=list)
    closure: RetainedClosureResumption = field(default_factory=RetainedClosureResumption)
    ordinary_stops: bool = False

    def resolve_two_tier(self, github_client, repository, pr_data):
        self.events.append("identity")
        return object()

    def resume_retained_closure(self, repository, pr_number, head_sha, inputs, github_client):
        self.events.append("closure")
        return self.closure

    def resume_ordinary_correction(self, request, inputs):
        self.events.append("ordinary")
        return self.ordinary_stops

    def repair_strong_findings(self, request, inputs):
        self.events.append("strong")
        return False

    def record_identity_deferral(self, pr_number, head_sha, reason):
        self.events.append("identity-deferral")

    def record_closure_deferral(self, pr_number, head_sha, reason):
        self.events.append("closure-deferral")

    def assess_saved(self, request, saved_status, claimed_review_threads):
        raise AssertionError("not used")

    def replay_saved(self, request, report, projection):
        raise AssertionError("not used")


def request(actions, status=None, *, force=False):
    return SavedReviewRequest(object(), "owner/repo", {"number": 7}, "head", force, AutomationConfig(), actions, status)


def test_pre_admission_resumption_preserves_phase_order():
    ports = Ports()
    result = SavedReviewResumptionService(ports).resume_before_admission(request(Actions()))

    assert not result.stop_processing
    assert ports.events == ["identity", "closure", "ordinary", "strong"]


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


def test_force_does_not_discard_a_usable_retained_ordinary_correction():
    ports = Ports(ordinary_stops=True)

    result = SavedReviewResumptionService(ports).resume_before_admission(request(Actions(), force=True))

    assert result.stop_processing
    assert ports.events == ["identity", "closure", "ordinary"]
