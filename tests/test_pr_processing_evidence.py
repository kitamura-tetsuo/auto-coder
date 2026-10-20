import pytest

from auto_coder.local_llm_observability import observe_local_llm_call
from auto_coder.pr_processing_evidence import bind_pr_repair_purpose, observe_pr_processing, observed_invocations


def dispatch(*, backend="codex", is_noedit=False, fails=False):
    with observe_local_llm_call(
        backend=backend,
        backend_type=backend,
        provider="local-cli",
        model="test-model",
        invocation_id=f"invocation-{backend}",
        continuation=False,
        is_noedit=is_noedit,
    ):
        if fails:
            raise RuntimeError("provider failed")


def test_actual_editing_dispatch_is_recorded_without_manager_inference():
    with observe_pr_processing("owner/repo", 42):
        with bind_pr_repair_purpose("owner/repo", 42, "local-ci-repair"):
            dispatch()

    assert [(item.backend_alias, item.status, item.purpose) for item in observed_invocations()] == [("codex", "returned", "local-ci-repair")]


def test_noedit_and_wrong_target_dispatches_are_not_editing_witnesses():
    with observe_pr_processing("owner/repo", 42):
        with bind_pr_repair_purpose("owner/repo", 42, "local-ci-repair"):
            dispatch(is_noedit=True)
        with bind_pr_repair_purpose("owner/repo", 43, "local-ci-repair-follow-up"):
            dispatch(backend="gemini")

    assert observed_invocations() == ()


def test_failed_dispatch_retains_attempt_without_claiming_return():
    with observe_pr_processing("owner/repo", 42):
        with bind_pr_repair_purpose("owner/repo", 42, "local-conflict-repair"):
            with pytest.raises(RuntimeError, match="provider failed"):
                dispatch(backend="gemini", fails=True)

    evidence = observed_invocations()
    assert len(evidence) == 1
    assert (evidence[0].backend_alias, evidence[0].status) == ("gemini", "failed")
