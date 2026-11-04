import hashlib
import json

import pytest

from auto_coder.management.beta_runtime_smoke import LocalSmokeEvidence
from auto_coder.management.source_acceptance import SourceAcceptanceError, SourceAcceptancePublisher
from auto_coder.util.gh_cache import GitDataResponse

SHA = "a" * 40
OID = "b" * 40


class Git:
    def __init__(self, *, lose_ref_response: bool = False) -> None:
        self.tag = None
        self.ref = None
        self.lose = lose_ref_response

    def request(self, method, path, payload=None):
        if method == "POST" and path == "git/tags":
            self.tag = {**payload, "sha": OID}
            return GitDataResponse(201, {"sha": OID})
        if method == "POST" and path == "git/refs":
            self.ref = payload
            if self.lose:
                raise RuntimeError("lost response")
            return GitDataResponse(201, payload)
        if path.startswith("git/ref/"):
            if self.ref is None:
                return GitDataResponse(404)
            return GitDataResponse(200, {"ref": self.ref["ref"], "object": {"type": "tag", "sha": self.ref["sha"]}})
        if path == f"git/tags/{OID}" and self.tag is not None:
            return GitDataResponse(
                200,
                {
                    **self.tag,
                    "object": {"type": self.tag["type"], "sha": self.tag["object"]},
                },
            )
        return GitDataResponse(404)


def evidence(result="PASS"):
    content = json.dumps(
        {"schema_version": 1, "record_type": "beta-runtime-smoke", "operation_id": "accept-1", "repository": "owner/repo", "source_sha": SHA, "local_evidence_id": "beta-runtime-smoke-v1:accept-1", "local_result": result, "capture": {"source": {"repository": "owner/repo", "source_sha": SHA}}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return LocalSmokeEvidence("beta-runtime-smoke-v1:accept-1", hashlib.sha256(content.encode()).hexdigest(), content)


@pytest.mark.parametrize("lost", [False, True])
def test_exact_create_only_tag_readback_derives_publication(lost):
    git = Git(lose_ref_response=lost)
    publisher = SourceAcceptancePublisher(git, "owner/repo")
    result = publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    assert result.direct_commit_sha == SHA
    assert result.tag_object_oid == OID
    assert result == publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    assert "PASS_PUBLISHED" not in result.annotation


def test_non_pass_local_result_is_never_published():
    git = Git()
    with pytest.raises(SourceAcceptanceError, match="exact PASS"):
        SourceAcceptancePublisher(git, "owner/repo").publish(evidence("BLOCKED"), "2026-01-01T00:00:00Z")
    assert git.tag is None


def test_existing_mismatched_tag_is_a_collision():
    git = Git()
    publisher = SourceAcceptancePublisher(git, "owner/repo")
    publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    git.tag["message"] = git.tag["message"].replace(SHA, "c" * 40)
    with pytest.raises(SourceAcceptanceError, match="differs|conflicts"):
        publisher.publish(evidence(), "2026-01-01T00:00:00Z")


def test_readback_rejects_annotation_with_substituted_local_evidence():
    git = Git()
    publisher = SourceAcceptancePublisher(git, "owner/repo")
    publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    annotation = json.loads(git.tag["message"])
    annotation["local_evidence"]["capture"]["source"]["source_sha"] = "c" * 40
    git.tag["message"] = json.dumps(annotation, sort_keys=True, separators=(",", ":"))
    with pytest.raises(SourceAcceptanceError, match="digest differs"):
        publisher.read("accept-1")
