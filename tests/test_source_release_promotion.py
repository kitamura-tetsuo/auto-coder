"""Original producer/publisher to source-release transaction boundary regression."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from auto_coder.beta_source_selection import MutationCertainty
from auto_coder.cli_commands_deployment import deployment_group
from auto_coder.management.beta_runtime_smoke import LocalSmokeEvidence, SmokeProfile, SmokeState
from auto_coder.management.source_acceptance import SourceAcceptancePublisher
from auto_coder.source_release_catalog import (
    RELEASE_SELECTION_REF,
    RELEASE_WORKFLOW,
    AllowedProfile,
    SourceReleaseCatalog,
    SourceReleaseConflict,
    SourceReleaseError,
    SourceReleasePolicy,
    SourceReleaseRequest,
    canonical,
    digest,
)
from auto_coder.source_release_promotion import SourceReleasePromotion
from auto_coder.tested_source_refs import WORKFLOW_PATH
from auto_coder.tested_source_refs import TestedSourcePublisher as SourceProofPublisher
from auto_coder.util.gh_cache import GitDataResponse
from tests.test_beta_runtime_smoke import _fixture
from tests.test_beta_source_selection import RealGitDataBoundary, selector
from tests.test_management_worker_lifecycle import GitEvidenceBoundary

SOURCE_SHA = "a" * 40
OTHER_SHA = "b" * 40
RELEASE_REF = "refs/tags/auto-coder-release-source-current"


class ReleaseGit(GitEvidenceBoundary):
    """Stateful Git/Actions/Release REST boundary; no predeclared success."""

    def __init__(self) -> None:
        super().__init__()
        self.releases: dict[str, dict[str, object]] = {}
        self.release_pointer_writes = 0
        self.release_selection_writes = 0
        self.lost_release_pointer_response = False
        self.lost_release_response = False
        self.unreadable_pointer = False
        self.supersede_before_ref_write = False
        self.catalog_read_lost = False
        self.catalog_read_lost_after_create = False
        self.catalog_create_response_lost = False
        self.move_source_after_release_read = False

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        if method == "GET" and path.startswith("actions/runs/900/attempts/") and not path.endswith("jobs?per_page=100"):
            self.calls.append((method, path))
            attempt = int(path.rsplit("/", 1)[1])
            return GitDataResponse(200, {"id": 900, "run_attempt": attempt, "path": RELEASE_WORKFLOW, "head_branch": "main", "event": "workflow_dispatch", "status": "in_progress", "head_sha": SOURCE_SHA, "repository": {"full_name": "owner/repo"}})
        if method == "GET" and path.startswith("releases/tags/"):
            self.calls.append((method, path))
            record = self.releases.get(path.removeprefix("releases/tags/"))
            if record is not None and self.move_source_after_release_read:
                self.refs[RELEASE_REF] = ("commit", OTHER_SHA)
            return GitDataResponse(200, record) if record is not None else GitDataResponse(404)
        if method == "POST" and path == "releases" and payload is not None:
            self.calls.append((method, path))
            name = str(payload["tag_name"])
            ref = f"refs/tags/{name}"
            assert ref in self.refs
            if name in self.releases:
                return GitDataResponse(422)
            self.releases[name] = {"id": len(self.releases) + 1, "tag_name": name, "draft": False, "prerelease": False, "html_url": f"https://github.com/owner/repo/releases/tag/{name}"}
            if self.lost_release_response:
                raise RuntimeError("response lost after release publication")
            return GitDataResponse(201, self.releases[name])
        if path == "git/ref/tags/auto-coder-release-source-current" and self.unreadable_pointer:
            self.calls.append((method, path))
            return GitDataResponse(503)
        if method == "POST" and path == "git/refs" and payload and payload.get("ref") == RELEASE_REF:
            self.release_pointer_writes += 1
            self.calls.append((method, path))
            if self.supersede_before_ref_write:
                self.refs[RELEASE_REF] = ("commit", OTHER_SHA)
                return GitDataResponse(422)
            self.refs[RELEASE_REF] = ("commit", str(payload["sha"]))
            if self.lost_release_pointer_response:
                raise RuntimeError("response lost after release source ref creation")
            return GitDataResponse(201)
        if method == "POST" and path == "git/refs" and payload and payload.get("ref") == RELEASE_SELECTION_REF:
            self.release_selection_writes += 1
            return super().request(method, path, payload)
        if method == "PATCH" and path == "git/refs/tags/auto-coder-release-source-selection-current" and payload:
            self.release_selection_writes += 1
            self.calls.append((method, path))
            assert payload["force"] is True
            self.refs[RELEASE_SELECTION_REF] = ("tag", str(payload["sha"]))
            return GitDataResponse(200)
        if method == "PATCH" and path == "git/refs/tags/auto-coder-release-source-current" and payload:
            self.release_pointer_writes += 1
            self.calls.append((method, path))
            if self.supersede_before_ref_write:
                self.refs[RELEASE_REF] = ("commit", OTHER_SHA)
                return GitDataResponse(409)
            assert payload["force"] is True
            self.refs[RELEASE_REF] = ("commit", str(payload["sha"]))
            if self.lost_release_pointer_response:
                raise RuntimeError("response lost after release source ref update")
            return GitDataResponse(200)
        if self.catalog_read_lost and method == "GET" and path.startswith("git/ref/tags/auto-coder-source-release-"):
            self.calls.append((method, path))
            return GitDataResponse(503)
        if method == "POST" and path == "git/refs" and payload and str(payload.get("ref", "")).startswith("refs/tags/auto-coder-source-release-"):
            result = super().request(method, path, payload)
            if self.catalog_read_lost_after_create:
                self.catalog_read_lost = True
            if self.catalog_create_response_lost:
                raise RuntimeError("lost source catalog ref creation acknowledgement")
            return result
        return super().request(method, path, payload)


class RealReleaseGit(RealGitDataBoundary):
    """Real annotated Git objects/refs with controlled Actions/Release REST."""

    def __init__(self, repository: Path) -> None:
        super().__init__(repository)
        self.releases: dict[str, dict[str, object]] = {}
        self.release_pointer_writes = 0
        self.release_selection_writes = 0
        self.lose_update_response = False

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        if method == "GET" and path.startswith("actions/runs/900/attempts/") and not path.endswith("jobs?per_page=100"):
            attempt = int(path.rsplit("/", 1)[1])
            return GitDataResponse(200, {"id": 900, "run_attempt": attempt, "path": RELEASE_WORKFLOW, "head_branch": "main", "event": "workflow_dispatch", "status": "in_progress", "head_sha": self.executions[(11, 1)][0], "repository": {"full_name": "owner/repo"}})
        if method == "GET" and path.startswith("releases/tags/"):
            found = self.releases.get(path.removeprefix("releases/tags/"))
            return GitDataResponse(200, found) if found else GitDataResponse(404)
        if method == "POST" and path == "releases" and payload:
            name = str(payload["tag_name"])
            assert self._git("cat-file", "-t", self._git("rev-parse", f"refs/tags/{name}")) == "tag"
            if name in self.releases:
                return GitDataResponse(422)
            self.releases[name] = {"id": 1, "tag_name": name, "draft": False, "prerelease": False, "html_url": f"https://github.com/owner/repo/releases/tag/{name}"}
            return GitDataResponse(201, self.releases[name])
        if method == "PATCH" and path == "git/refs/tags/auto-coder-beta-selection-current" and payload:
            self._git("update-ref", "refs/tags/auto-coder-beta-selection-current", str(payload["sha"]))
            return GitDataResponse(200)
        if method == "PATCH" and path == "git/refs/tags/auto-coder-release-source-current" and payload:
            assert payload["force"] is True
            self.release_pointer_writes += 1
            self._git("update-ref", RELEASE_REF, str(payload["sha"]))
            if self.lose_update_response:
                raise RuntimeError("lost update response after actual git update-ref")
            return GitDataResponse(200)
        if method == "PATCH" and path == "git/refs/tags/auto-coder-release-source-selection-current" and payload:
            assert payload["force"] is True
            self.release_selection_writes += 1
            self._git("update-ref", RELEASE_SELECTION_REF, str(payload["sha"]))
            return GitDataResponse(200)
        if method == "POST" and path == "git/refs" and payload and payload.get("ref") == RELEASE_SELECTION_REF:
            self.release_selection_writes += 1
            return super().request(method, path, payload)
        if method == "POST" and path == "git/refs" and payload and payload.get("ref") == RELEASE_REF:
            self.release_pointer_writes += 1
            result = super().request(method, path, payload)
            if self.lose_update_response:
                raise RuntimeError("lost create response after actual git update-ref")
            return result
        result = super().request(method, path, payload)
        if method == "GET" and path.endswith("/jobs?per_page=100") and isinstance(result.data, dict):
            jobs = result.data.get("jobs")
            if isinstance(jobs, list):
                for index, job in enumerate(jobs, start=1):
                    job["id"] = index + 100
        return result


def policy(profile_revision: str, maximum_age: int = 86400) -> SourceReleasePolicy:
    return SourceReleasePolicy(
        1,
        "owner/repo",
        "release",
        RELEASE_REF,
        ("beta-worker",),
        (AllowedProfile("beta-runtime-smoke-v1", profile_revision),),
        maximum_age,
        WORKFLOW_PATH,
        RELEASE_WORKFLOW,
        "main",
    )


def _harness(tmp_path: Path, remote=None):
    remote = remote or ReleaseGit()
    smoke = _fixture(tmp_path, remote=remote)
    first = smoke.advance()
    assert first.state is SmokeState.IN_PROGRESS, first.reason
    smoke.next_heartbeat()
    result = smoke.advance()
    assert result.state is SmokeState.COMPLETE and result.evidence is not None
    acceptance = SourceAcceptancePublisher(remote, "owner/repo").publish(result.evidence, "2026-11-08T00:00:00Z")  # type: ignore[arg-type]
    tested = SourceProofPublisher(remote, "owner/repo").read(smoke.worker.source_sha)  # type: ignore[arg-type]
    assert tested is not None
    request = SourceReleaseRequest(
        "promotion-1",
        "owner/repo",
        smoke.worker.source_sha,
        tested.record.run_id,
        tested.record.run_attempt,
        tested.tag_ref,
        tested.tag_object_oid,
        digest(asdict(tested.record)),
        acceptance.tag_ref,
        acceptance.tag_object_oid,
        "release",
        900,
        1,
    )
    remote.calls.clear()
    return remote, smoke, request, policy(smoke.profile.content_revision)


def _corrupt_annotation(remote: ReleaseGit, request: SourceReleaseRequest, change) -> None:
    tag = remote.tags[request.acceptance_oid]
    annotation = json.loads(str(tag["message"]))
    local = annotation["local_evidence"]
    change(local)
    annotation["local_evidence_content_sha256"] = hashlib.sha256(canonical(local).encode()).hexdigest()
    tag["message"] = canonical(annotation)


def _publication_calls(remote: ReleaseGit) -> list[tuple[str, str]]:
    return [(method, path) for method, path in remote.calls if method in {"POST", "PATCH"} and (path.startswith("git/") or path == "releases")]


def test_original_producer_publisher_promotes_only_verified_git_source(tmp_path: Path) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    outcome = SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    assert outcome.status == "CONFIRMED"
    assert outcome.record.schema_version == 2
    assert outcome.record.source_sha == SOURCE_SHA
    assert outcome.record.local_evidence_content_sha256 == json.loads(remote.tags[request.acceptance_oid]["message"])["local_evidence_content_sha256"]
    assert "image" not in outcome.record.serialize() and "digest" not in outcome.record.serialize()
    assert remote.refs[RELEASE_REF] == ("commit", SOURCE_SHA)
    assert remote.refs[f"refs/tags/{outcome.record.tag}"] == ("tag", outcome.catalog_oid)
    history_ref = remote.calls.index(("POST", "git/refs"))
    pointer_effect = next(i for i, item in enumerate(remote.calls) if item in {("POST", "git/refs"), ("PATCH", "git/refs/tags/auto-coder-release-source-current")} and i > history_ref)
    assert history_ref < pointer_effect
    assert remote.release_pointer_writes == 1
    assert not any("docker" in path or "packages" in path for _method, path in remote.calls)
    remote.calls.clear()
    replay = SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(replace(request, promotion_run_attempt=2))  # type: ignore[arg-type]
    assert replay.status == "ALREADY_COMPLETED"
    assert remote.release_pointer_writes == 1
    assert _publication_calls(remote) == []


@pytest.mark.parametrize("fault", ["heartbeat", "startup", "gate", "runtime", "deadline", "profile", "local_result", "ready"])
def test_adversarial_published_pass_is_rejected_before_any_release_effect(tmp_path: Path, fault: str) -> None:
    remote, smoke, request, configured = _harness(tmp_path)

    def mutate(local):
        if fault == "heartbeat":
            local["samples"][1]["heartbeat"]["sequence"] = local["samples"][0]["heartbeat"]["sequence"]
        elif fault == "startup":
            local["samples"][1]["lifecycle"]["startup_reconciliation_error"] = "startup failed"
        elif fault == "gate":
            local["samples"][1]["checks"][4]["disposition"] = "FAIL"
        elif fault == "runtime":
            local["samples"][1]["runtime_binding"]["runtime_id"] = "changed-runtime"
        elif fault == "deadline":
            local["window"]["deadline"] = local["samples"][1]["sampled_at"]
        elif fault == "profile":
            local["profile"]["content_revision"] = "a" * 64
        elif fault == "local_result":
            local["local_result"] = "BLOCKED"
        elif fault == "ready":
            local["capture"]["deployment"]["staged"]["content_identity"] = "a" * 64

    _corrupt_annotation(remote, request, mutate)
    with pytest.raises(SourceReleaseError):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    assert _publication_calls(remote) == []
    assert RELEASE_REF not in remote.refs


@pytest.mark.parametrize("fault", ["missing_ref", "wrong_oid", "wrong_source", "stale", "disallowed_instance", "disallowed_profile", "failed_original_ci", "missing_original_ci"])
def test_eligibility_mismatch_is_refused_without_publication(tmp_path: Path, fault: str) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    if fault == "missing_ref":
        remote.refs.pop(request.acceptance_ref)
    elif fault == "wrong_oid":
        request = replace(request, acceptance_oid="e" * 40)
    elif fault == "wrong_source":
        request = replace(request, source_sha=OTHER_SHA, tested_source_ref=f"refs/tags/auto-coder-tested-{OTHER_SHA}")
    elif fault == "stale":
        configured = policy(smoke.profile.content_revision, maximum_age=1)
    elif fault == "disallowed_instance":
        configured = replace(configured, accepted_beta_instance_ids=("other-beta",))
    elif fault == "disallowed_profile":
        configured = replace(configured, accepted_profiles=(AllowedProfile("beta-runtime-smoke-v1", "d" * 64),))
    elif fault == "failed_original_ci":
        remote.failed_attempts.add((101, 1))
    elif fault == "missing_original_ci":
        remote.proofs.clear()
        remote.executions.pop((101, 1), None)
        remote.tags.pop(request.tested_source_oid, None)
    with pytest.raises(SourceReleaseError):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 30).execute(request)  # type: ignore[arg-type]
    assert _publication_calls(remote) == []


@pytest.mark.parametrize("lost", [False, True])
def test_lost_release_pointer_and_publication_responses_are_read_back(tmp_path: Path, lost: bool) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    remote.lost_release_pointer_response = lost
    remote.lost_release_response = lost
    outcome = SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    assert outcome.status == "CONFIRMED"
    assert remote.release_pointer_writes == 1
    assert len(remote.releases) == 1
    assert SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(replace(request, promotion_run_attempt=2)).status == "ALREADY_COMPLETED"  # type: ignore[arg-type]
    assert remote.release_pointer_writes == 1


def test_external_ref_winner_and_missing_readback_are_not_claimed_atomic(tmp_path: Path) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    remote.refs[RELEASE_REF] = ("commit", OTHER_SHA)
    remote.supersede_before_ref_write = True
    with pytest.raises(SourceReleaseError, match="unconfirmed|superseded"):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    assert remote.release_pointer_writes == 1
    assert not remote.releases
    remote.supersede_before_ref_write = False
    with pytest.raises(SourceReleaseError, match="NEEDS_ATTENTION"):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(replace(request, promotion_run_attempt=2))  # type: ignore[arg-type]
    assert remote.release_pointer_writes == 1


def test_missing_catalog_or_unreadable_release_ref_prevents_mutation(tmp_path: Path) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    with pytest.raises(SourceReleaseError, match="retried Actions attempt"):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(replace(request, promotion_run_attempt=2))  # type: ignore[arg-type]
    remote.unreadable_pointer = True
    with pytest.raises(SourceReleaseError, match="unavailable"):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    assert _publication_calls(remote) == []


def test_operation_collision_cannot_change_original_candidate(tmp_path: Path) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    remote.calls.clear()
    with pytest.raises(SourceReleaseError, match="candidate|evidence|operation"):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(replace(request, tested_source_record_sha256="f" * 64, promotion_run_attempt=2))  # type: ignore[arg-type]
    assert _publication_calls(remote) == []
    assert remote.release_pointer_writes == 1


def test_concurrent_identical_catalog_creation_does_not_grant_second_pointer_writer(tmp_path: Path, monkeypatch) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    promotion = SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1)  # type: ignore[arg-type]
    original_prepare = promotion.catalog.prepare

    def race(record, *, require_new=False):
        SourceReleaseCatalog(remote, configured.source_repository).prepare(record)  # type: ignore[arg-type]
        return original_prepare(record, require_new=require_new)

    monkeypatch.setattr(promotion.catalog, "prepare", race)
    with pytest.raises(SourceReleaseConflict, match="appeared after first-attempt admission"):
        promotion.execute(request)
    assert remote.release_pointer_writes == 0
    assert not remote.releases


def test_lost_catalog_create_response_and_readback_preserve_single_selection(tmp_path: Path) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    remote.catalog_create_response_lost = True
    completed = SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    assert completed.status == "CONFIRMED" and remote.release_pointer_writes == 1

    separate, smoke2, request2, configured2 = _harness(tmp_path / "separate")
    separate.catalog_read_lost_after_create = True
    with pytest.raises(SourceReleaseError, match="unavailable"):
        SourceReleasePromotion(separate, configured2, clock=lambda: smoke2.clock.now + 1).execute(request2)  # type: ignore[arg-type]
    assert separate.release_pointer_writes == 0
    separate.catalog_read_lost = False
    with pytest.raises(SourceReleaseError, match="NEEDS_ATTENTION"):
        SourceReleasePromotion(separate, configured2, clock=lambda: smoke2.clock.now + 1).execute(replace(request2, promotion_run_attempt=2))  # type: ignore[arg-type]
    assert separate.release_pointer_writes == 0


def test_external_change_after_release_publication_cannot_claim_completed(tmp_path: Path) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    remote.move_source_after_release_read = True
    with pytest.raises(SourceReleaseError, match="SUPERSEDED"):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]
    assert remote.release_pointer_writes == 1
    assert len(remote.releases) == 1


def test_policy_rejects_caller_owned_authority_and_legacy_source_format(tmp_path: Path) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    path = tmp_path / "policy.json"
    path.write_text(canonical(asdict(configured)))
    assert SourceReleasePolicy.load(path) == configured
    path.write_text(path.read_text().replace('"release_branch":"main"', '"release_branch":"other"'))
    with pytest.raises(SourceReleaseError, match="trusted Actions"):
        SourceReleasePolicy.load(path)
    assert configured.revision != replace(configured, maximum_evidence_age_seconds=1).revision
    with pytest.raises(SourceReleaseError, match="repository or release target"):
        replace(request, source_repository="attacker/repo").validate(configured)
    assert "digest" not in canonical(asdict(configured))


def test_workflow_uses_fixed_main_dispatch_and_shared_release_writer_lane() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / RELEASE_WORKFLOW).read_text())
    legacy = yaml.safe_load((root / ".github/workflows/promote-release.yml").read_text())
    rollback = yaml.safe_load((root / ".github/workflows/rollback-release.yml").read_text())
    assert workflow["run-name"] == "accepted-release/${{ inputs.operation_id }}"
    assert workflow["concurrency"] == legacy["concurrency"] == rollback["concurrency"] == {"group": "release-channel", "cancel-in-progress": False}
    assert workflow["permissions"] == {"contents": "write", "actions": "read"}
    job = workflow["jobs"]["promote-accepted-git-source"]
    assert "if" not in job
    assert "refs/heads/main" in job["steps"][0]["run"]
    assert "exit 1" in job["steps"][0]["run"]
    run = job["steps"][-1]["run"]
    assert "${{ inputs." not in run and "docker" not in run
    assert '--acceptance-oid "$ACCEPTANCE_OID"' in run
    assert '--promotion-run-attempt "$GITHUB_RUN_ATTEMPT"' in run
    assert "GITHUB_TOKEN" in job["steps"][-1]["env"]
    checked_in_policy = SourceReleasePolicy.load(root / ".github/accepted-source-release-policy.json")
    assert checked_in_policy.accepted_profiles == (AllowedProfile("beta-runtime-smoke-v1", SmokeProfile().content_revision),)


def test_cli_refuses_malformed_input_before_git_mutation(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(deployment_group, ["promote-accepted-release", "--operation-id", "bad; id"])
    assert result.exit_code != 0


def test_cli_dispatches_fixed_policy_and_exact_source_identity(tmp_path: Path, monkeypatch) -> None:
    remote, smoke, request, configured = _harness(tmp_path)
    policy_file = tmp_path / "operator-owned-policy.json"
    policy_file.write_text(canonical(asdict(configured)), encoding="utf-8")
    monkeypatch.setattr("auto_coder.cli_commands_deployment.GitHubGitDataClient", lambda *_args: remote)
    monkeypatch.setattr("auto_coder.cli_commands_deployment.SourceReleasePromotion", lambda client, policy: SourceReleasePromotion(client, policy, clock=lambda: smoke.clock.now + 1))
    values = {
        "operation-id": request.operation_id,
        "source-repository": request.source_repository,
        "source-sha": request.source_sha,
        "ci-run-id": str(request.ci_run_id),
        "ci-run-attempt": str(request.ci_run_attempt),
        "tested-source-ref": request.tested_source_ref,
        "tested-source-oid": request.tested_source_oid,
        "tested-source-record-sha256": request.tested_source_record_sha256,
        "acceptance-ref": request.acceptance_ref,
        "acceptance-oid": request.acceptance_oid,
        "release-target": request.release_target,
        "promotion-run-id": str(request.promotion_run_id),
        "promotion-run-attempt": str(request.promotion_run_attempt),
        "policy-path": str(policy_file),
    }
    arguments = ["promote-accepted-release"]
    for name, value in values.items():
        arguments.extend(("--" + name, value))
    result = CliRunner().invoke(deployment_group, arguments, env={"GITHUB_TOKEN": "test-token"})
    assert result.exit_code == 0, result.output
    assert result.output.startswith("CONFIRMED: auto-coder-source-release-promotion-1")
    assert remote.refs[RELEASE_REF] == ("commit", SOURCE_SHA)


def _another_real_commit(remote: RealReleaseGit, label: str) -> str:
    return remote._git(
        "commit-tree",
        remote._git("mktree", input=""),
        input=label + "\n",
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"},
    )


def test_actual_git_annotation_and_two_source_commits_survive_moved_beta_and_lost_ref_reply(tmp_path: Path, _use_real_commands: None) -> None:
    remote = RealReleaseGit(tmp_path / "source.git")
    original = remote.executions[(11, 1)][0]
    second = _another_real_commit(remote, "different real tested source")
    assert original != second
    remote._git("update-ref", RELEASE_REF, second)
    remote, smoke, request, configured = _harness(tmp_path / "smoke", remote)

    # A later, different commit replaces beta only after the original local
    # PASS and its immutable acceptance publication have completed.
    remote.executions[(12, 1)] = (second, "2026-10-02T02:00:00Z")
    remote.add_proof(second, "", 102, 1)
    assert selector(remote).advance("r78", original).certainty is MutationCertainty.CONFIRMED
    assert remote._git("rev-parse", "refs/tags/auto-coder-beta-current") == second
    remote.lose_update_response = True
    outcome = SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(request)  # type: ignore[arg-type]

    assert outcome.status == "CONFIRMED"
    assert remote.release_pointer_writes == 1
    assert remote._git("rev-parse", RELEASE_REF) == original
    assert remote._git("cat-file", "-t", outcome.catalog_oid) == "tag"
    assert remote._git("rev-parse", f"refs/tags/{outcome.record.tag}^{{}}") == original
    annotation = remote._git("cat-file", "-p", outcome.catalog_oid).split("\n\n", 1)[1]
    assert annotation == outcome.record.serialize()
    assert remote._git("cat-file", "-t", request.acceptance_oid) == "tag"
    assert remote._git("rev-parse", request.acceptance_ref) == request.acceptance_oid
    assert SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(replace(request, promotion_run_attempt=2)).status == "ALREADY_COMPLETED"  # type: ignore[arg-type]
    assert remote.release_pointer_writes == 1


@pytest.mark.parametrize("fault", ["nonadvancing", "failed_readiness"])
def test_real_annotated_pass_with_forged_original_samples_cannot_publish(tmp_path: Path, _use_real_commands: None, fault: str) -> None:
    remote = RealReleaseGit(tmp_path / "source.git")
    remote, smoke, request, configured = _harness(tmp_path / "smoke", remote)
    original = request.source_sha
    original_tag = remote._git("cat-file", "-p", request.acceptance_oid).split("\n\n", 1)[1]
    document = json.loads(original_tag)
    local = document["local_evidence"]
    if fault == "nonadvancing":
        local["samples"][1]["heartbeat"]["sequence"] = local["samples"][0]["heartbeat"]["sequence"]
    else:
        local["samples"][1]["lifecycle"]["startup_reconciled"] = False
    document["local_evidence_content_sha256"] = hashlib.sha256(canonical(local).encode()).hexdigest()
    name = request.acceptance_ref.removeprefix("refs/tags/")
    forged_oid = remote._git("mktag", input=f"object {original}\ntype commit\ntag {name}\ntagger test <test@example.com> 0 +0000\n\n{canonical(document)}\n")
    remote._git("update-ref", request.acceptance_ref, forged_oid)
    forged_request = replace(request, acceptance_oid=forged_oid)

    with pytest.raises(SourceReleaseError, match="heartbeat|lifecycle"):
        SourceReleasePromotion(remote, configured, clock=lambda: smoke.clock.now + 1).execute(forged_request)  # type: ignore[arg-type]
    assert remote.release_pointer_writes == 0
    with pytest.raises(subprocess.CalledProcessError):
        remote._git("show-ref", "--verify", RELEASE_REF)
    absent = remote.request("GET", "git/ref/tags/auto-coder-source-release-promotion-1")
    assert absent.status == 404
