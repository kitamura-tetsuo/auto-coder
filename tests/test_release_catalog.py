import json
from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from auto_coder.cli_commands_deployment import deployment_group
from auto_coder.release_catalog import ReleaseCatalog, ReleaseCatalogConflict, ReleaseCatalogError, new_record, parse_record
from auto_coder.util.gh_cache import GitDataResponse

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


@dataclass
class StatefulGitData:
    tags: dict[str, dict[str, object]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, object] | None]] = field(default_factory=list)
    lose_ref_response: bool = False

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("git/ref/tags/"):
            name = path.removeprefix("git/ref/tags/")
            sha = self.refs.get(name)
            return GitDataResponse(200, {"ref": f"refs/tags/{name}", "object": {"type": "tag", "sha": sha}}) if sha else GitDataResponse(404, {"message": "Not Found"})
        if method == "GET" and path.startswith("git/tags/"):
            tag = self.tags.get(path.removeprefix("git/tags/"))
            return GitDataResponse(200, tag) if tag else GitDataResponse(404, {})
        if method == "POST" and path == "git/tags" and payload is not None:
            sha = f"{len(self.tags) + 1:040x}"
            self.tags[sha] = {**payload, "sha": sha, "object": {"type": payload["type"], "sha": payload["object"]}}
            return GitDataResponse(201, {"sha": sha})
        if method == "POST" and path == "git/refs" and payload is not None:
            name = str(payload["ref"]).removeprefix("refs/tags/")
            if name in self.refs:
                return GitDataResponse(422, {})
            self.refs[name] = str(payload["sha"])
            if self.lose_ref_response:
                raise RuntimeError("response lost")
            return GitDataResponse(201, {"ref": payload["ref"]})
        raise AssertionError((method, path, payload))


def proposal(run_id: int = 123, memo: str = "Webhook変更前", requested_at: str = "2026-09-07T08:49:12Z"):
    return new_record("owner/repo", run_id, requested_at, SHA, DIGEST, memo)


def test_prepare_and_new_process_read_complete_record() -> None:
    remote = StatefulGitData()
    prepared = ReleaseCatalog(remote, "owner/repo").prepare(proposal())
    assert prepared.release_tag == "release-20260907T174912JST-r123"
    assert remote.refs[prepared.release_tag] in remote.tags
    assert json.loads(str(remote.tags[remote.refs[prepared.release_tag]]["message"])) == prepared.__dict__
    assert ReleaseCatalog(remote, "owner/repo").read(prepared.release_tag) == prepared


def test_retry_collision_rollover_and_no_ref_rewrite() -> None:
    remote = StatefulGitData()
    catalog = ReleaseCatalog(remote, "owner/repo")
    first = catalog.prepare(proposal())
    assert catalog.prepare(proposal()) == first
    assert len([call for call in remote.calls if call[:2] == ("POST", "git/refs")]) == 1
    assert proposal(124).release_tag.endswith("-r124")
    assert proposal(125, requested_at="2026-09-07T16:00:00Z").release_tag == "release-20260908T010000JST-r125"


def test_lost_response_is_accepted_only_after_readback() -> None:
    remote = StatefulGitData(lose_ref_response=True)
    assert ReleaseCatalog(remote, "owner/repo").prepare(proposal()) == proposal()
    assert remote.calls[-2][0:2] == ("GET", f"git/ref/tags/{proposal().release_tag}")


def test_conflicting_race_never_updates_or_deletes_winner() -> None:
    remote = StatefulGitData()
    catalog = ReleaseCatalog(remote, "owner/repo")
    catalog.prepare(proposal(memo="winner"))
    with pytest.raises(ReleaseCatalogConflict):
        catalog.prepare(proposal(memo="loser"))
    assert all(call[0] not in {"PATCH", "DELETE", "PUT"} for call in remote.calls)


@pytest.mark.parametrize(
    "message",
    [
        '{"schema_version":1,"schema_version":1}',
        proposal().to_json().replace('"schema_version":1', '"schema_version":2'),
        proposal().to_json()[:-1] + ',"extra":true}',
        proposal().__class__(**{**proposal().__dict__, "repository": "other/repo"}).to_json(),
    ],
)
def test_reader_rejects_duplicate_unknown_cross_repo_and_schema(message: str) -> None:
    with pytest.raises(ReleaseCatalogError):
        parse_record(message, "owner/repo")


def test_invalid_proposal_causes_no_mutation_and_memo_is_inert() -> None:
    remote = StatefulGitData()
    with pytest.raises(ReleaseCatalogError):
        new_record("Owner/repo", 1, "not-a-date", "bad", "bad", "")
    assert remote.calls == []
    memo = '日本語\n"quoted" `code` $(touch /tmp/catalog-injection) {"digest":"evil"}'
    assert ReleaseCatalog(remote, "owner/repo").prepare(proposal(memo=memo)).memo == memo


def test_lightweight_or_indirect_tag_and_unavailable_read_fail_closed() -> None:
    remote = StatefulGitData(refs={proposal().release_tag: SHA})
    with pytest.raises(ReleaseCatalogError, match="annotated"):
        ReleaseCatalog(remote, "owner/repo").read(proposal().release_tag)

    class Unavailable:
        def request(self, method: str, path: str, payload=None) -> GitDataResponse:
            return GitDataResponse(403, {"message": "forbidden"})

    with pytest.raises(ReleaseCatalogError, match="unavailable"):
        ReleaseCatalog(Unavailable(), "owner/repo").prepare(proposal())


def test_production_cli_serializes_complete_record(monkeypatch: pytest.MonkeyPatch) -> None:
    remote = StatefulGitData()
    monkeypatch.setattr("auto_coder.cli_commands_deployment.GitHubGitDataClient", lambda token, repository, api_url: remote)
    runner = CliRunner()
    args = ["release-catalog", "prepare", "--repository", "owner/repo", "--run-id", "123", "--requested-at", "2026-09-07T08:49:12Z", "--source-sha", SHA, "--digest", DIGEST, "--memo", "Webhook変更前", "--github-token", "token"]
    prepared = runner.invoke(deployment_group, args)
    assert prepared.exit_code == 0, prepared.output
    record = json.loads(prepared.output)
    read = runner.invoke(deployment_group, ["release-catalog", "read", "--repository", "owner/repo", "--release-tag", record["release_tag"], "--github-token", "token"])
    assert read.exit_code == 0, read.output
    assert json.loads(read.output) == record
    assert {path for _, path, _ in remote.calls if path.startswith("releases") or path.startswith("packages")} == set()
