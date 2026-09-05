"""Tests for durable logical implementation ownership."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationOwnerResolutionError,
    ImplementationSlotRepository,
    ImplementationSlotUnavailable,
)
from auto_coder.llm_backend_config import get_max_concurrent_implementations_from_config
from auto_coder.util.gh_cache import GitHubClient


class GitHubState:
    def __init__(self, issues=None, prs=None, linked_prs=None, open_prs=None):
        self.issues = issues or {}
        self.prs = prs or {}
        self.linked_prs = linked_prs or {}
        self.open_prs = open_prs or []

    def get_issue(self, _repo, number):
        value = self.issues[number]
        if isinstance(value, Exception):
            raise value
        return value

    def get_issue_details(self, issue):
        return issue

    def get_pull_request(self, _repo, number):
        return self.prs[number]

    def get_pr_details(self, pr):
        return pr

    def get_linked_prs(self, _repo, issue_number, strict=False):
        return self.linked_prs.get(issue_number, [])

    def get_open_pull_requests(self, _repo):
        return self.open_prs


def repository(tmp_path, limit=1):
    return ImplementationSlotRepository("owner/repo", limit, tmp_path / "slots.json")


def execution_process(storage_path, owner_kind="issue", owner_number=100, force=False, lifetime=60):
    """Start an execution through the production repository API in another process."""
    script = """
import os
import time
from pathlib import Path
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

slots = ImplementationSlotRepository("owner/repo", 1, Path(os.environ["SLOT_PATH"]))
execution_id = slots.start_execution(
    ImplementationOwner(os.environ["OWNER_KIND"], int(os.environ["OWNER_NUMBER"])),
    bypass_capacity=True,
    bypass_active_execution=os.environ["FORCE"] == "1",
)
print(execution_id, flush=True)
time.sleep(float(os.environ["LIFETIME"]))
"""
    environment = os.environ.copy()
    environment.update(
        SLOT_PATH=str(storage_path),
        OWNER_KIND=owner_kind,
        OWNER_NUMBER=str(owner_number),
        FORCE="1" if force else "0",
        LIFETIME=str(lifetime),
    )
    process = subprocess.Popen([sys.executable, "-c", script], env=environment, stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    execution_id = process.stdout.readline().strip()
    assert execution_id and execution_id != "None"
    return process, execution_id


def wait_for_zombie(process, timeout=5):
    """Wait for a child to terminate without reaping its procfs record."""
    deadline = time.monotonic() + timeout
    stat_path = f"/proc/{process.pid}/stat"
    while time.monotonic() < deadline:
        stat = open(stat_path, encoding="ascii").read()
        state = stat[stat.rfind(")") + 2 :].split()[0]
        if state == "Z":
            return
        time.sleep(0.01)
    pytest.fail(f"process {process.pid} did not enter zombie state")


def unix_identity_command(uid, gid):
    identity = ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups"]
    return identity if os.geteuid() == 0 else ["sudo", "-n", *identity]


def run_slot_writer_as(uid, gid, storage_path, owner_number, action="reserve"):
    """Run one production repository update under a selected Unix identity."""
    script = """
import json
import sys
from pathlib import Path
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

slots = ImplementationSlotRepository("owner/repo", 10, Path(sys.argv[1]))
owner = ImplementationOwner("issue", int(sys.argv[2]))
action = sys.argv[3]
if action == "serialize-cycle":
    execution_id = slots.start_execution(owner, bypass_capacity=True)
    assert execution_id
    with slots.serialize(owner):
        pass
    slots.finish_execution(owner, execution_id)
elif action == "update":
    assert slots.record_provider_session(owner, "cross-identity-update")
else:
    assert slots.reserve(owner)
print(json.dumps({
    "owners": [item.number for item in slots.active_owners()],
    "executions": list(slots.active_execution_ids(owner)),
}))
"""
    return subprocess.run(
        [*unix_identity_command(uid, gid), sys.executable, "-c", script, str(storage_path), str(owner_number), action],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )


def test_alternating_unix_identities_retain_shared_permissions_and_state():
    """REQ-001/REQ-002/REQ-003: atomic writes remain usable across OS identities."""
    shared_directory = Path(tempfile.mkdtemp(prefix="auto-coder-shared-slots-", dir="/tmp"))
    storage_path = shared_directory / "implementation_slots.json"
    inaccessible_path = shared_directory / "inaccessible.json"
    inaccessible_path.write_text('{"issue:9": {"kind": "issue", "number": 9}}', encoding="utf-8")
    first_uid, second_uid, shared_gid = 65532, 65533, os.getgid()
    ownership_command = ["chown", f"{os.getuid()}:{shared_gid}", str(shared_directory)]
    if os.geteuid() != 0:
        ownership_command = ["sudo", "-n", *ownership_command]
    try:
        subprocess.run(ownership_command, check=True)
        mode_command = ["chmod", "2770", str(shared_directory)]
        if os.geteuid() != 0:
            mode_command = ["sudo", "-n", *mode_command]
        subprocess.run(mode_command, check=True)

        assert json.loads(run_slot_writer_as(first_uid, shared_gid, storage_path, 101).stdout)["owners"] == [101]
        assert json.loads(run_slot_writer_as(second_uid, shared_gid, storage_path, 202).stdout)["owners"] == [101, 202]
        assert json.loads(run_slot_writer_as(first_uid, shared_gid, storage_path, 303).stdout)["owners"] == [101, 202, 303]
        assert json.loads(run_slot_writer_as(second_uid, shared_gid, storage_path, 404).stdout)["owners"] == [101, 202, 303, 404]

        for uid in (first_uid, second_uid, first_uid):
            result = json.loads(run_slot_writer_as(uid, shared_gid, storage_path, 505, "serialize-cycle").stdout)
            assert result["executions"] == []

        state = json.loads(storage_path.read_text(encoding="utf-8"))
        assert set(state) == {"issue:101", "issue:202", "issue:303", "issue:404", "issue:505"}
        owner_lock_path = shared_directory / "implementation-issue-505.lock"
        for path in (storage_path, storage_path.with_suffix(".lock"), owner_lock_path):
            metadata = path.stat()
            assert metadata.st_gid == shared_gid
            assert metadata.st_mode & 0o777 == 0o660

        live_script = """
import sys
import time
from pathlib import Path
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

slots = ImplementationSlotRepository("owner/repo", 10, Path(sys.argv[1]))
execution_id = slots.start_execution(ImplementationOwner("issue", 606))
assert execution_id
print(execution_id, flush=True)
time.sleep(30)
"""
        live_process = subprocess.Popen(
            [*unix_identity_command(first_uid, shared_gid), sys.executable, "-c", live_script, str(storage_path)],
            cwd=Path(__file__).parents[1],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert live_process.stdout is not None
        legacy_execution_id = live_process.stdout.readline().strip()
        assert legacy_execution_id
        try:
            legacy_mode_command = ["chmod", "640", str(storage_path)]
            if os.geteuid() != 0:
                legacy_mode_command = ["sudo", "-n", *legacy_mode_command]
            subprocess.run(legacy_mode_command, check=True)

            migrated = json.loads(run_slot_writer_as(second_uid, shared_gid, storage_path, 606, "update").stdout)
            assert migrated["executions"] == [legacy_execution_id]
            migrated_state = json.loads(storage_path.read_text(encoding="utf-8"))
            assert migrated_state["issue:606"]["executions"][0]["id"] == legacy_execution_id
            assert migrated_state["issue:606"]["provider_sessions"] == ["cross-identity-update"]
            assert storage_path.stat().st_mode & 0o777 == 0o660
        finally:
            live_process.terminate()
            live_process.wait(timeout=5)

        restrict_command = ["chown", f"{first_uid}:{shared_gid}", str(inaccessible_path)]
        if os.geteuid() != 0:
            restrict_command = ["sudo", "-n", *restrict_command]
        subprocess.run(restrict_command, check=True)
        restrict_command = ["chmod", "600", str(inaccessible_path)]
        if os.geteuid() != 0:
            restrict_command = ["sudo", "-n", *restrict_command]
        subprocess.run(restrict_command, check=True)
        with pytest.raises(subprocess.CalledProcessError) as failure:
            run_slot_writer_as(second_uid, shared_gid, inaccessible_path, 10)
        assert "Cannot safely establish or use implementation slot shared-state permissions" in failure.value.stderr
        assert str(inaccessible_path) in failure.value.stderr
        read_command = ["cat", str(inaccessible_path)]
        if os.geteuid() != 0:
            read_command = ["sudo", "-n", *read_command]
        preserved_state = subprocess.run(read_command, check=True, capture_output=True, text=True).stdout
        assert json.loads(preserved_state) == {"issue:9": {"kind": "issue", "number": 9}}
    finally:
        cleanup_command = ["rm", "-rf", str(shared_directory)]
        if os.geteuid() != 0:
            cleanup_command = ["sudo", "-n", *cleanup_command]
        subprocess.run(cleanup_command, check=True)


def test_readable_legacy_state_is_migrated_without_semantic_reset(tmp_path):
    owner = ImplementationOwner("issue", 1749)
    storage_path = tmp_path / "slots.json"
    storage_path.write_text(json.dumps({owner.key: {"kind": owner.kind, "number": owner.number}}), encoding="utf-8")
    os.chmod(storage_path, 0o600)

    slots = ImplementationSlotRepository("owner/repo", 2, storage_path)

    assert slots.active_owners() == (owner,)
    assert slots.reserve(ImplementationOwner("issue", 1750)) is True
    assert storage_path.stat().st_mode & 0o777 == 0o660
    assert storage_path.with_suffix(".lock").stat().st_mode & 0o777 == 0o660


def test_corrupt_state_error_is_distinct_from_permission_failures(tmp_path):
    slots = repository(tmp_path)
    slots.storage_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ImplementationSlotUnavailable, match="Cannot safely parse implementation slot state"):
        slots.active_owners()


def test_cross_identity_processes_racing_final_slot_admit_exactly_one():
    """TOG-40a52233e11d: flock serializes production admission across processes."""
    shared_directory = Path(tempfile.mkdtemp(prefix="auto-coder-slot-race-", dir="/tmp"))
    storage_path = shared_directory / "implementation_slots.json"
    gate_path = shared_directory / "start"
    first_uid, second_uid, shared_gid = 65532, 65533, os.getgid()
    setup_commands = [
        ["chown", f"{os.getuid()}:{shared_gid}", str(shared_directory)],
        ["chmod", "2770", str(shared_directory)],
    ]
    race_script = """
import sys
import time
from pathlib import Path
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

storage_path, gate_path, owner_number = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
print("READY", flush=True)
while not gate_path.exists():
    time.sleep(0.005)
execution_id = ImplementationSlotRepository("owner/repo", 1, storage_path).start_execution(
    ImplementationOwner("issue", owner_number)
)
print(execution_id or "DENIED", flush=True)
time.sleep(30)
"""
    processes = []
    try:
        for command in setup_commands:
            if os.geteuid() != 0:
                command = ["sudo", "-n", *command]
            subprocess.run(command, check=True)
        for uid, owner_number in ((first_uid, 701), (second_uid, 702)):
            process = subprocess.Popen(
                [*unix_identity_command(uid, shared_gid), sys.executable, "-c", race_script, str(storage_path), str(gate_path), str(owner_number)],
                cwd=Path(__file__).parents[1],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            processes.append(process)
        assert [process.stdout.readline().strip() for process in processes] == ["READY", "READY"]

        gate_path.touch()
        outcomes = [process.stdout.readline().strip() for process in processes]

        assert outcomes.count("DENIED") == 1
        assert sum(outcome != "DENIED" for outcome in outcomes) == 1
        state = json.loads(storage_path.read_text(encoding="utf-8"))
        assert len(state) == 1
        assert set(state).issubset({"issue:701", "issue:702"})
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)
        cleanup_command = ["rm", "-rf", str(shared_directory)]
        if os.geteuid() != 0:
            cleanup_command = ["sudo", "-n", *cleanup_command]
        subprocess.run(cleanup_command, check=True)


def test_reservation_is_durable_and_independent_owners_obey_limit(tmp_path):
    first = repository(tmp_path)
    assert first.reserve(ImplementationOwner("issue", 100)) is True

    after_restart = repository(tmp_path)
    assert after_restart.active_owners() == (ImplementationOwner("issue", 100),)
    assert after_restart.reserve(ImplementationOwner("issue", 200)) is False


def test_related_pr_reuses_issue_owner_and_multiple_prs_count_once(tmp_path):
    github = GitHubState(issues={100: {"number": 100, "state": "open"}})
    slots = repository(tmp_path)
    issue = slots.resolve_owner("issue", {"number": 100}, github)
    first_pr = slots.resolve_owner("pr", {"number": 105, "body": "Closes #100"}, github)
    second_pr = slots.resolve_owner("pr", {"number": 108, "body": "Fixes #100"}, github)

    assert issue == first_pr == second_pr == ImplementationOwner("issue", 100)
    assert slots.reserve(issue) is True
    assert slots.reserve(first_pr) is True
    assert slots.active_owners() == (issue,)


def test_standalone_pr_has_independent_owner(tmp_path):
    slots = repository(tmp_path)
    github = GitHubState()
    assert slots.reserve(ImplementationOwner("issue", 100)) is True
    owner = slots.resolve_owner("pr", {"number": 200, "body": "No linked issue"}, github)
    assert owner == ImplementationOwner("pr", 200)
    assert slots.reserve(owner) is False


def test_source_less_provider_pr_reuses_durable_recurrent_owner(tmp_path):
    slots = repository(tmp_path)
    recurrent_owner = ImplementationOwner("recurrent", 12345)
    assert slots.reserve_new(recurrent_owner) is True
    assert slots.record_provider_session(recurrent_owner, "session-abc") is True

    github = GitHubState()
    owner_from_session = slots.resolve_owner(
        "pr",
        {"number": 123, "body": "Created by https://jules.google.com/session/session-abc"},
        github,
    )
    assert owner_from_session == recurrent_owner
    assert slots.reserve(owner_from_session) is True

    assert slots.record_implementation_pr(recurrent_owner, 123) is True
    owner_after_restart = repository(tmp_path).resolve_owner(
        "pr",
        {"number": 123, "body": "No source Issue or session metadata"},
        github,
    )
    assert owner_after_restart == recurrent_owner


def test_startup_records_open_provider_pr_membership(tmp_path):
    slots = repository(tmp_path)
    recurrent_owner = ImplementationOwner("recurrent", 12345)
    assert slots.reserve_new(recurrent_owner) is True
    assert slots.record_provider_session(recurrent_owner, "session-abc") is True
    github = GitHubState(
        open_prs=[
            {
                "number": 123,
                "body": "Created by https://jules.google.com/session/session-abc",
            }
        ]
    )

    slots.reconcile(github, discover_open_prs=True)

    restarted = repository(tmp_path)
    owner = restarted.resolve_owner(
        "pr",
        {"number": 123, "body": "No source Issue or provider metadata"},
        GitHubState(),
    )
    assert owner == recurrent_owner
    assert restarted.reserve(ImplementationOwner("issue", 200)) is False


def test_reconciliation_releases_terminal_but_retains_uncertain_owner(tmp_path):
    slots = repository(tmp_path, limit=2)
    terminal = ImplementationOwner("issue", 100)
    uncertain = ImplementationOwner("issue", 200)
    slots.reserve(terminal)
    slots.reserve(uncertain)
    github = GitHubState(issues={100: {"state": "closed"}, 200: RuntimeError("offline")})

    slots.reconcile(github)

    assert slots.active_owners() == (uncertain,)


def test_closed_issue_retains_slot_while_sibling_pr_is_open(tmp_path):
    slots = repository(tmp_path)
    issue_owner = ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner) is True
    github = GitHubState(
        issues={100: {"number": 100, "state": "closed"}},
        prs={
            105: {"number": 105, "state": "closed", "merged": True, "body": "Closes #100"},
            108: {"number": 108, "state": "open", "merged": False, "body": "Fixes #100"},
        },
        linked_prs={100: [105, 108]},
    )

    slots.reconcile(github)

    assert slots.active_owners() == (issue_owner,)
    assert slots.reserve(ImplementationOwner("issue", 200)) is False
    sibling_owner = slots.resolve_owner("pr", github.prs[108], github)
    assert sibling_owner == issue_owner
    assert slots.reserve(sibling_owner) is True
    assert slots.active_owners() == (issue_owner,)


def test_reconciliation_retains_slot_when_production_timeline_is_unavailable(tmp_path, monkeypatch):
    slots = repository(tmp_path)
    issue_owner = ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner) is True
    github = GitHubClient("token")
    monkeypatch.setattr(github, "get_issue", lambda _repo, _number: {"number": 100, "state": "closed"})
    monkeypatch.setattr(github, "get_issue_details", lambda issue: issue)

    class UnavailableTimeline:
        def get(self, _url, headers=None):
            raise RuntimeError("timeline offline")

    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda: UnavailableTimeline())

    slots.reconcile(github)

    assert slots.active_owners() == (issue_owner,)
    assert slots.reserve(ImplementationOwner("issue", 200)) is False


def test_reconciliation_reads_all_timeline_pages_before_releasing_slot(tmp_path, monkeypatch):
    slots = repository(tmp_path)
    issue_owner = ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner) is True
    github = GitHubClient("token")
    monkeypatch.setattr(github, "get_issue", lambda _repo, _number: {"number": 100, "state": "closed"})
    monkeypatch.setattr(github, "get_issue_details", lambda issue: issue)
    monkeypatch.setattr(
        GitHubClient,
        "get_issue_strict",
        lambda _self, _repo, number: {"number": number, "state": "closed"},
    )
    monkeypatch.setattr(
        github,
        "get_pull_request",
        lambda _repo, number: {"number": number, "state": "open", "merged": False},
    )
    monkeypatch.setattr(github, "get_pr_details", lambda pr: pr)

    first_url = "https://api.github.com/repos/owner/repo/issues/100/timeline?per_page=100"
    second_url = f"{first_url}&page=2"

    class TimelineResponse:
        def __init__(self, events, next_url=None):
            self.events = events
            self.links = {"next": {"url": next_url}} if next_url else {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.events

    class PaginatedTimeline:
        def __init__(self):
            self.requested_urls = []

        def get(self, url, headers=None):
            self.requested_urls.append(url)
            if url == first_url:
                return TimelineResponse([{"event": "commented"}] * 100, second_url)
            assert url == second_url
            return TimelineResponse(
                [
                    {
                        "event": "cross-referenced",
                        "source": {"issue": {"number": 108, "pull_request": {}}},
                    }
                ]
            )

    timeline = PaginatedTimeline()
    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda: timeline)

    slots.reconcile(github)

    assert timeline.requested_urls == [first_url, second_url]
    assert slots.active_owners() == (issue_owner,)
    assert slots.reserve(ImplementationOwner("issue", 200)) is False
    sibling_owner = slots.resolve_owner("pr", {"number": 108, "body": "Fixes #100"}, github)
    assert sibling_owner == issue_owner
    assert slots.reserve(sibling_owner) is True


def test_reconciliation_retains_branch_linked_pr_absent_from_timeline(tmp_path, monkeypatch):
    slots = repository(tmp_path)
    github = GitHubClient("token")
    monkeypatch.setattr(
        GitHubClient,
        "get_issue_strict",
        lambda _self, _repo, number: {"number": number, "state": "closed"},
    )
    monkeypatch.setattr(github, "get_issue", lambda _repo, number: {"number": number, "state": "closed"})
    monkeypatch.setattr(github, "get_issue_details", lambda issue: issue)
    monkeypatch.setattr(
        github,
        "get_pull_request",
        lambda _repo, number: {"number": number, "state": "open", "merged": False},
    )
    monkeypatch.setattr(github, "get_pr_details", lambda pr: pr)

    class EmptyTimelineResponse:
        links = {}

        def raise_for_status(self):
            return None

        def json(self):
            return []

    class EmptyTimeline:
        def get(self, _url, headers=None):
            return EmptyTimelineResponse()

    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda: EmptyTimeline())
    branch_linked_pr = {
        "number": 108,
        "title": "Implementation",
        "body": "",
        "head": {"ref": "issue-100-work"},
    }
    issue_owner = slots.resolve_owner("pr", branch_linked_pr, github)
    assert issue_owner == ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner, implementation_pr=108) is True

    restarted_slots = repository(tmp_path)
    restarted_slots.reconcile(github)

    assert restarted_slots.active_owners() == (issue_owner,)
    assert restarted_slots.reserve(ImplementationOwner("issue", 200)) is False
    assert restarted_slots.resolve_owner("pr", branch_linked_pr, github) == issue_owner
    assert restarted_slots.reserve(issue_owner, implementation_pr=108) is True


def test_startup_reconciliation_discovers_unrecorded_branch_linked_pr(tmp_path):
    slots = repository(tmp_path)
    issue_owner = ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner) is True

    class StartupGitHub(GitHubState):
        def get_open_pull_requests(self, _repo):
            return [
                {
                    "number": 108,
                    "title": "Implementation",
                    "body": "",
                    "head": {"ref": "issue-100-work"},
                }
            ]

    github = StartupGitHub(
        issues={100: {"number": 100, "state": "closed", "title": "Source Issue"}},
        prs={108: {"number": 108, "state": "open", "merged": False}},
        linked_prs={100: []},
    )

    restarted_slots = repository(tmp_path)
    restarted_slots.reconcile(github, discover_open_prs=True)

    assert restarted_slots.active_owners() == (issue_owner,)
    state = json.loads((tmp_path / "slots.json").read_text(encoding="utf-8"))
    assert state["issue:100"]["implementation_prs"] == [108]
    assert restarted_slots.reserve(ImplementationOwner("issue", 200)) is False
    assert restarted_slots.reserve(issue_owner, implementation_pr=108) is True


def test_pr_resolution_failure_is_not_treated_as_standalone(tmp_path):
    slots = repository(tmp_path)
    github = GitHubState(issues={100: RuntimeError("offline")})
    with pytest.raises(ImplementationOwnerResolutionError, match="offline"):
        slots.resolve_owner("pr", {"number": 105, "body": "Closes #100"}, github)


def test_atomic_reservation_never_exceeds_limit(tmp_path):
    owners = [ImplementationOwner("issue", number) for number in range(10)]

    def reserve(owner):
        return repository(tmp_path).reserve(owner)

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = list(executor.map(reserve, owners))

    assert outcomes.count(True) == 1
    assert len(repository(tmp_path).active_owners()) == 1


def test_reserve_new_rejects_existing_owner_and_occupied_capacity(tmp_path):
    slots = repository(tmp_path, limit=1)
    owner = ImplementationOwner("recurrent", 10)

    assert slots.reserve_new(owner) is True
    assert slots.reserve_new(owner) is False
    assert slots.reserve_new(ImplementationOwner("issue", 200)) is False
    assert slots.active_owners() == (owner,)


def test_only_execution_bypasses_capacity_but_not_active_duplicate(tmp_path):
    slots = repository(tmp_path)
    first = ImplementationOwner("issue", 100)
    second = ImplementationOwner("issue", 200)
    first_execution = slots.start_execution(first)

    assert first_execution is not None
    second_execution = slots.start_execution(second, bypass_capacity=True)
    assert second_execution is not None
    assert slots.start_execution(second, bypass_capacity=True) is None
    assert set(slots.active_owners()) == {first, second}

    restarted = repository(tmp_path)
    assert restarted.active_execution_ids(first) == (first_execution,)
    assert restarted.active_execution_ids(second) == (second_execution,)
    assert restarted.start_execution(ImplementationOwner("issue", 300)) is None


def test_forced_execution_has_distinct_identity_and_scoped_cleanup(tmp_path):
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    execution_a = slots.start_execution(owner)
    execution_b = slots.start_execution(owner, bypass_capacity=True, bypass_active_execution=True)

    assert execution_a is not None
    assert execution_b is not None
    assert execution_a != execution_b
    assert slots.active_execution_ids(owner) == (execution_a, execution_b)

    slots.finish_execution(owner, execution_b)

    assert repository(tmp_path).active_execution_ids(owner) == (execution_a,)


def test_urgent_emergency_is_atomic_durable_and_does_not_consume_normal_capacity(tmp_path):
    slots = repository(tmp_path, limit=3)
    normal_owners = [ImplementationOwner("issue", number) for number in (1, 2, 3)]
    for owner in normal_owners:
        assert slots.start_execution(owner) is not None

    urgent_owners = [ImplementationOwner("issue", 10), ImplementationOwner("issue", 11)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda owner: repository(tmp_path, limit=3).start_execution(owner, allow_urgent_emergency=True), urgent_owners))

    assert sum(result is not None for result in results) == 1
    restarted = repository(tmp_path, limit=3)
    assert len(restarted.active_owners()) == 4
    assert restarted.start_execution(ImplementationOwner("issue", 12), allow_urgent_emergency=True) is None
    assert restarted.start_execution(ImplementationOwner("issue", 13)) is None


def test_emergency_owner_leaves_normal_capacity_available_and_allowance_is_reusable(tmp_path):
    slots = repository(tmp_path, limit=3)
    normal_owners = [ImplementationOwner("issue", number) for number in (1, 2, 3)]
    for owner in normal_owners:
        assert slots.start_execution(owner) is not None
    emergency = ImplementationOwner("issue", 10)
    assert slots.start_execution(emergency, allow_urgent_emergency=True) is not None
    slots.release(normal_owners[2])
    assert slots.start_execution(ImplementationOwner("issue", 4)) is not None
    assert slots.start_execution(ImplementationOwner("issue", 5)) is None

    slots.release(emergency)
    assert slots.start_execution(ImplementationOwner("issue", 12), allow_urgent_emergency=True) is not None


def test_urgent_emergency_does_not_bypass_duplicate_execution_guard(tmp_path):
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 1)
    assert slots.start_execution(owner) is not None
    assert slots.start_execution(owner, allow_urgent_emergency=True) is None


def test_reconcile_does_not_release_owner_with_an_active_execution(tmp_path):
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    execution = slots.start_execution(owner)
    github = GitHubState(issues={100: {"state": "closed"}}, linked_prs={100: []})

    slots.reconcile(github)

    assert slots.active_owners() == (owner,)
    assert slots.active_execution_ids(owner) == (execution,)


def test_crashed_execution_is_reclaimed_before_issue_lifecycle_reconciliation(tmp_path):
    """AC-001: the production execution origin survives a crash, then startup cleanup releases a terminal owner."""
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    process, execution_id = execution_process(slots.storage_path)
    process.terminate()
    process.wait(timeout=5)
    github = GitHubState(issues={100: {"state": "closed"}}, linked_prs={100: []}, open_prs=[])

    repository(tmp_path).reconcile(github, discover_open_prs=True)

    assert execution_id not in repository(tmp_path).active_execution_ids(owner)
    assert repository(tmp_path).active_owners() == ()


def test_crashed_execution_on_open_issue_is_retried_without_releasing_owner(tmp_path):
    """AC-002: admission reclaims a dead duplicate while retaining its logical owner."""
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    process, stale_execution = execution_process(slots.storage_path)
    process.terminate()
    process.wait(timeout=5)

    replacement = repository(tmp_path).start_execution(owner)

    assert replacement is not None
    assert replacement != stale_execution
    assert repository(tmp_path).active_owners() == (owner,)
    assert repository(tmp_path).active_execution_ids(owner) == (replacement,)


def test_unreaped_zombie_execution_is_reclaimed_during_retry_admission(tmp_path):
    """REQ-001/REQ-003/REQ-007: a terminated procfs identity cannot block retry."""
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    process, stale_execution = execution_process(slots.storage_path, lifetime=0)
    try:
        wait_for_zombie(process)

        replacement = repository(tmp_path).start_execution(owner)

        assert replacement is not None
        assert replacement != stale_execution
        assert repository(tmp_path).active_execution_ids(owner) == (replacement,)
        assert repository(tmp_path).active_owners() == (owner,)
    finally:
        process.wait(timeout=5)


@pytest.mark.parametrize("owner", [ImplementationOwner("issue", 100), ImplementationOwner("pr", 200)])
def test_unreaped_zombie_execution_allows_terminal_owner_release(tmp_path, owner):
    """REQ-005/REQ-006: reconciliation treats terminated zombie tasks as stale."""
    slots = repository(tmp_path)
    process, execution_id = execution_process(slots.storage_path, owner_kind=owner.kind, owner_number=owner.number, lifetime=0)
    try:
        wait_for_zombie(process)
        github = GitHubState(
            issues={owner.number: {"number": owner.number, "state": "closed"}},
            prs={owner.number: {"number": owner.number, "state": "closed", "merged": False}},
            linked_prs={owner.number: []},
        )

        repository(tmp_path).reconcile(github)

        assert execution_id not in repository(tmp_path).active_execution_ids(owner)
        assert repository(tmp_path).active_owners() == ()
    finally:
        process.wait(timeout=5)


def test_live_execution_in_another_process_survives_startup_reconciliation(tmp_path):
    """AC-003/AC-007: a restart cannot clear another process's live identity."""
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    process, execution_id = execution_process(slots.storage_path)
    try:
        github = GitHubState(issues={100: {"state": "closed"}}, linked_prs={100: []}, open_prs=[])
        restarted = repository(tmp_path)

        restarted.reconcile(github, discover_open_prs=True)

        assert restarted.active_execution_ids(owner) == (execution_id,)
        assert restarted.active_owners() == (owner,)
        assert restarted.start_execution(owner) is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("owner", [ImplementationOwner("issue", 100), ImplementationOwner("pr", 200)])
def test_execution_admitted_during_terminal_lifecycle_lookup_is_not_released(tmp_path, owner):
    """REQ-002/REQ-009: final owner release atomically rechecks execution state."""
    reconciling = repository(tmp_path)
    admitting = repository(tmp_path)
    assert reconciling.reserve(owner) is True
    lifecycle_lookup_started = threading.Event()
    permit_lifecycle_result = threading.Event()

    class PausedTerminalGitHub(GitHubState):
        def _terminal_item(self, number):
            assert number == owner.number
            lifecycle_lookup_started.set()
            assert permit_lifecycle_result.wait(timeout=5)
            return {"number": number, "state": "closed", "merged": False}

        def get_issue(self, _repo, number):
            return self._terminal_item(number)

        def get_pull_request(self, _repo, number):
            return self._terminal_item(number)

    github = PausedTerminalGitHub(linked_prs={owner.number: []})
    with ThreadPoolExecutor(max_workers=1) as executor:
        reconciliation = executor.submit(reconciling.reconcile, github)
        assert lifecycle_lookup_started.wait(timeout=5)
        execution_id = admitting.start_execution(owner)
        assert execution_id is not None
        permit_lifecycle_result.set()
        reconciliation.result(timeout=5)

    assert repository(tmp_path).active_owners() == (owner,)
    assert repository(tmp_path).active_execution_ids(owner) == (execution_id,)


def test_pr_membership_recorded_during_reconciliation_prevents_owner_release(tmp_path):
    """REQ-005/REQ-009: final release rejects a stale PR-membership snapshot."""
    reconciling = repository(tmp_path)
    discovering = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    assert reconciling.reserve(owner) is True
    linked_pr_lookup_started = threading.Event()
    permit_linked_pr_result = threading.Event()

    class PausedMembershipGitHub(GitHubState):
        def __init__(self):
            super().__init__(issues={100: {"number": 100, "state": "closed"}}, linked_prs={100: []})
            self.requested_prs = []

        def get_linked_prs(self, _repo, issue_number, strict=False):
            assert issue_number == owner.number
            assert strict is True
            linked_pr_lookup_started.set()
            assert permit_linked_pr_result.wait(timeout=5)
            return []

        def get_pull_request(self, _repo, number):
            self.requested_prs.append(number)
            return {"number": number, "state": "open", "merged": False}

    github = PausedMembershipGitHub()
    with ThreadPoolExecutor(max_workers=1) as executor:
        reconciliation = executor.submit(reconciling.reconcile, github)
        assert linked_pr_lookup_started.wait(timeout=5)
        assert discovering.record_implementation_pr(owner, 101) is True
        permit_linked_pr_result.set()
        reconciliation.result(timeout=5)

    state = json.loads(reconciling.storage_path.read_text(encoding="utf-8"))
    assert reconciling.active_owners() == (owner,)
    assert state[owner.key]["implementation_prs"] == [101]
    assert github.requested_prs == []

    # The next lifecycle pass consumes the newly durable membership and retains
    # the owner based on the PR's authoritative open state.
    reconciling.reconcile(github)

    assert github.requested_prs == [101]
    assert reconciling.active_owners() == (owner,)


def test_crashed_execution_is_reclaimed_before_standalone_pr_release(tmp_path):
    """AC-004: a terminal standalone PR releases capacity after its process crashes."""
    slots = repository(tmp_path)
    owner = ImplementationOwner("pr", 200)
    process, _execution_id = execution_process(slots.storage_path, owner_kind="pr", owner_number=200)
    process.terminate()
    process.wait(timeout=5)

    repository(tmp_path).reconcile(GitHubState(prs={200: {"state": "closed", "merged": False}}))

    assert repository(tmp_path).active_owners() == ()
    assert repository(tmp_path).start_execution(ImplementationOwner("issue", 300)) is not None


def test_execution_with_uncertain_liveness_is_retained(tmp_path):
    """AC-005: legacy or incomplete identities fail closed rather than assuming death."""
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    execution_id = slots.start_execution(owner)
    state = json.loads(slots.storage_path.read_text(encoding="utf-8"))
    state[owner.key]["executions"][0].pop("boot_id", None)
    state[owner.key]["executions"][0].pop("process_start_ticks", None)
    slots.storage_path.write_text(json.dumps(state), encoding="utf-8")

    slots.reconcile(GitHubState(issues={100: {"state": "closed"}}, linked_prs={100: []}))

    assert slots.active_execution_ids(owner) == (execution_id,)
    assert slots.active_owners() == (owner,)


def test_stale_cleanup_preserves_live_forced_sibling(tmp_path):
    """AC-006: cleanup changes only the conclusively dead execution record."""
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    stale_process, stale_execution = execution_process(slots.storage_path)
    live_process, live_execution = execution_process(slots.storage_path, force=True)
    try:
        stale_process.terminate()
        stale_process.wait(timeout=5)
        removed = repository(tmp_path).reclaim_stale_executions()

        assert removed == (stale_execution,)
        assert repository(tmp_path).active_execution_ids(owner) == (live_execution,)
    finally:
        if stale_process.poll() is None:
            stale_process.terminate()
            stale_process.wait(timeout=5)
        live_process.terminate()
        live_process.wait(timeout=5)


def test_configuration_rejects_non_positive_limit(tmp_path):
    with pytest.raises(ValueError, match="positive integer"):
        repository(tmp_path, limit=0)


@pytest.mark.parametrize("invalid_value", ["true", "1.5", '"1"'])
def test_configuration_rejects_non_integer_toml_values(tmp_path, invalid_value):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[process_issues]\nmax_concurrent_implementations = {invalid_value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="positive integer"):
        get_max_concurrent_implementations_from_config(str(config_path))


def test_same_owner_serialization_is_reentrant(tmp_path):
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    with slots.serialize(owner):
        with slots.serialize(owner):
            assert owner.key == "issue:100"
