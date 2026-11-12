"""Real host admission interruption probes for the isolated Docker fixture.

Only syscall/log observation barriers are installed in the producer process.
Every claim, grant adapter, flock, Docker identity and verifier is production
code. Original files remain in place after each deliberately interrupted case.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import stat
import subprocess
import sys
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management.fresh_initialization import FreshInitializationIntent
from auto_coder.management.original_attempts import enroll

_BARRIER_DRIVER = r"""
import json
import sys
from pathlib import Path
from loguru import logger
from auto_coder.management import fresh_host, original_attempts as origin

kind, checkpoint, history = sys.argv[1:4]
history = Path(history)
target = {"anchor": 0, "claimed": 1, "never-created": 1, "grant-possible": 2}[kind]
event_name = f"{target:08d}.json"
real_create, real_replace, real_sync = origin._create, origin.os.replace, origin._sync
written = replaced = False

def pause(label):
    print(label, flush=True)
    if sys.stdin.readline().strip() != "continue":
        raise SystemExit("test barrier was not explicitly released")

def barrier(point):
    if point == checkpoint:
        pause("BARRIER:" + point)

def create(root, name, value):
    global written
    selected = root == history and name == event_name
    if selected:
        barrier("before-event")
    real_create(root, name, value)
    if selected:
        written = True
        barrier("after-event")
    elif root == history and name == "frontier.pending" and written:
        barrier("after-pending")

def replace(source, destination):
    global replaced
    real_replace(source, destination)
    if Path(source) == history / "frontier.pending" and written:
        replaced = True
        barrier("after-replace")

def sync(root):
    real_sync(root)
    if root == history and replaced:
        barrier("after-sync")

def anchored(message):
    extra = message.record["extra"]
    if kind == "never-created" and extra.get("original_boundary") == "anchor":
        pause("ANCHOR:replace-oauth-path")

origin._create, origin.os.replace, origin._sync = create, replace, sync
logger.add(anchored)
sys.argv = ["fresh_host", *sys.argv[4:]]
fresh_host.main()
"""

# This continuation supplies real retained Docker observations and real current
# owner/network descriptors to the original production grant adapter. A broken
# refusal would open its socket and physically start the disposable container.
_GRANT_CONTINUATION = r"""
import json
import os
import subprocess
import sys
from pathlib import Path
from auto_coder.management.fresh_host import _evidence
from auto_coder.management.fresh_host_observation import serve_one_fresh_grant
from auto_coder.management.fresh_initialization import acquire_prior_owner_fence, parse_fresh_intent

intent = parse_fresh_intent(json.loads(Path(sys.argv[1]).read_bytes()))
management, proxy, _ = _evidence(Path(sys.argv[2]))
owner = acquire_prior_owner_fence(Path(sys.argv[3]), expected_device=intent.owner_lock_device, expected_inode=intent.owner_lock_inode)
try:
    serve_one_fresh_grant(
        Path(sys.argv[4]) / "fresh-guard.sock", intent, management, proxy,
        owner, int(sys.argv[5]), timeout=2,
        on_listening=lambda: subprocess.run(["docker", "start", intent.new_container_id], check=True, timeout=10),
    )
except Exception as error:
    print(type(error).__name__ + ": " + str(error), flush=True)
    raise SystemExit(7)
finally:
    os.close(owner)
"""


def _original_bytes(paths: tuple[Path, ...]) -> dict[str, tuple[int, int, int, str]]:
    observed = {}
    for root in paths:
        entries = [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]
        for path in entries:
            info = path.lstat()
            assert stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), path
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "directory"
            observed[str(path)] = (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), digest)
    return observed


def _assert_ready(process: subprocess.Popen[str], expected: str, log: Path) -> None:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 30)[0], log.read_text()[-4000:]
    actual = process.stdout.readline().strip()
    assert actual == expected, (actual, log.read_text()[-4000:])


def _continue(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.write("continue\n")
    process.stdin.flush()


def _run(command: list[str], network_fd: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, pass_fds=(network_fd,), capture_output=True, text=True, timeout=30, check=False)


def exercise_original_host_boundaries(
    root: Path,
    original: FreshInitializationIntent,
    kind: str,
    create_command: tuple[str, ...],
    network_fd: int,
    prior_lock: Path,
    evidence_file: Path,
    guard: Path,
    preserved: tuple[Path, ...],
    container_names: list[str],
) -> None:
    """Interrupt one boundary kind, then leave the main positive launch usable."""
    baseline = _original_bytes(preserved)
    scenarios = root / ("original-host-boundaries-" + kind)
    scenarios.mkdir(mode=0o700)
    main_state, main_journal = Path(original.host_state_root), Path(original.host_journal_root)
    cli = [sys.executable, "-B", "-m", "auto_coder.management.fresh_host"]
    host_arguments = ["--evidence-file", str(evidence_file), "--prior-lock", str(prior_lock), "--prior-network-fd", str(network_fd)]
    # Adjacent points are the same observable state: after event publication is
    # before pending publication; after pending publication is before replace.
    checkpoints = ("before-event", "after-event", "after-pending", "after-replace", "after-sync")
    for checkpoint in checkpoints:
        case = scenarios / checkpoint
        case.mkdir(mode=0o700)
        history, state, journal = (case / name for name in ("history", "state", "journal"))
        for directory in (history, state, journal):
            directory.mkdir(mode=0o700)
        binding = enroll(history, "isolated-host-boundary-" + kind + "-" + checkpoint)
        name = "fresh-original-boundary-" + uuid.uuid4().hex[:12]
        container_names.append(name)
        command = list(create_command)
        command[command.index("--name") + 1] = name
        for index, value in enumerate(command):
            if value.startswith("type=bind,src=" + str(main_state) + ","):
                command[index] = value.replace("src=" + str(main_state) + ",", "src=" + str(state) + ",", 1)
            elif value.startswith("type=bind,src=" + str(main_journal) + ","):
                command[index] = value.replace("src=" + str(main_journal) + ",", "src=" + str(journal) + ",", 1)
        container_id = subprocess.check_output(command, text=True, timeout=30).strip()
        state_info, journal_info = state.stat(), journal.stat()
        intent = replace(
            original,
            request_id=str(uuid.uuid4()),
            new_instance=name,
            new_container_id=container_id,
            host_state_root=str(state),
            host_journal_root=str(journal),
            state_device=state_info.st_dev,
            state_inode=state_info.st_ino,
            journal_device=journal_info.st_dev,
            journal_inode=journal_info.st_ino,
            original_history=binding.root,
            original_history_id=binding.history,
        )
        intent_file = case / "intent.json"
        intent_file.write_text(json.dumps(asdict(intent)))
        intent_file.chmod(0o600)
        claim_arguments = ["claim", "--intent-file", str(intent_file), *host_arguments]
        if kind == "grant-possible":
            claimed = _run([*cli, *claim_arguments], network_fd)
            assert claimed.returncode == 0, claimed.stderr
            arguments = ["guard", "--state-root", str(state), "--guard-dir", str(guard), *host_arguments]
        else:
            arguments = claim_arguments
        log = case / "producer.log"
        oauth = Path(original.host_oauth_root)
        parked_oauth = case / "preserved-oauth"
        with log.open("w") as errors:
            producer = subprocess.Popen(
                [sys.executable, "-B", "-c", _BARRIER_DRIVER, kind, checkpoint, binding.root, *arguments],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                pass_fds=(network_fd,),
            )
            try:
                if kind == "never-created":
                    _assert_ready(producer, "ANCHOR:replace-oauth-path", log)
                    # A real namespace intervention makes the native claim see
                    # a different OAuth inode after original anchor persistence.
                    # The retained database bytes and inode are never rewritten.
                    oauth.rename(parked_oauth)
                    oauth.mkdir(mode=0o700)
                    _continue(producer)
                _assert_ready(producer, "BARRIER:" + checkpoint, log)
                if parked_oauth.exists():
                    assert list(oauth.iterdir()) == []
                    oauth.rmdir()
                    parked_oauth.rename(oauth)
                assert _original_bytes(preserved) == baseline
                # Establish actual overlap, not merely simultaneous scheduling.
                with (history / "history.lock").open("rb") as probe:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                contender = replace(original, request_id=str(uuid.uuid4()), original_history=binding.root, original_history_id=binding.history)
                contender_file = case / "contender.json"
                contender_file.write_text(json.dumps(asdict(contender)))
                contender_file.chmod(0o600)
                contested = _run([*cli, "claim", "--intent-file", str(contender_file), *host_arguments], network_fd)
                assert contested.returncode == 1 and "history admission contested" in contested.stderr, (contested.stdout, contested.stderr)
                assert not list(main_state.iterdir()) and not list(main_journal.iterdir())
                producer.kill()
                producer.communicate(timeout=10)
                assert producer.returncode == -9
            finally:
                if producer.poll() is None:
                    producer.kill()
                    producer.communicate(timeout=10)
                if parked_oauth.exists():
                    assert list(oauth.iterdir()) == []
                    oauth.rmdir()
                    parked_oauth.rename(oauth)
        originals = _original_bytes((history, state, journal, *preserved))
        binding_file = case / "binding.json"
        binding_file.write_text(json.dumps(asdict(binding)))
        consumer_command = [sys.executable, "-B", "-m", "auto_coder.management.original_attempt_evidence", "--binding", str(binding_file)]
        consumer = _run(consumer_command, network_fd)
        evidence = json.loads(consumer.stdout)
        terminal_published = kind == "never-created" and checkpoint in {"after-replace", "after-sync"}
        if terminal_published:
            assert consumer.returncode == 0, consumer.stderr
            assert evidence["unavailable"] == []
            assert [(attempt["request_id"], attempt["state"]) for attempt in evidence["attempts"]] == [(intent.request_id, "never-created")]
            submitted = case / "submitted.json"
            submitted.write_text(consumer.stdout)
            verifier = _run([*consumer_command, "--submitted", str(submitted)], network_fd)
            assert verifier.returncode == 0, verifier.stderr
            assert json.loads(verifier.stdout) == evidence
            # SIGKILL after rename observes an atomic file publication; only
            # after-sync is the positive completed directory-fsync boundary.
            # Neither experiment purports to simulate storage power loss.
        else:
            assert consumer.returncode == 1 and evidence["unavailable"], (consumer.stdout, consumer.stderr)
            if checkpoint in {"after-event", "after-pending"}:
                assert "partial, omitted or conflicting frontier" in evidence["unavailable"]
        assert _original_bytes((history, state, journal, *preserved)) == originals
        # These three states permit a first grant in the contract; all other
        # interrupted/terminal states must refuse even a real launch callback.
        pending_first_grant = (kind == "claimed" and checkpoint in {"after-replace", "after-sync"}) or (kind == "grant-possible" and checkpoint == "before-event")
        if not pending_first_grant:
            continuation = _run(
                [sys.executable, "-B", "-c", _GRANT_CONTINUATION, str(intent_file), str(evidence_file), str(prior_lock), str(guard), str(network_fd)],
                network_fd,
            )
            assert continuation.returncode == 7, (continuation.stdout, continuation.stderr)
            assert "OriginalEvidenceUnavailable" in continuation.stdout, (continuation.stdout, continuation.stderr)
            assert "awaiting-replacement" not in continuation.stdout and "grant-delivered" not in continuation.stdout
            assert _original_bytes((history, state, journal, *preserved)) == originals
        # The separate consumer must release its temporary observation resource.
        with (history / "history.lock").open("rb") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not (guard / "fresh-guard.sock").exists()
        assert not list(journal.iterdir())
        assert {path.name for path in state.iterdir()} <= {"fresh-initialization.json"}
        inspected = json.loads(subprocess.check_output(["docker", "inspect", container_id], text=True, timeout=10))[0]
        assert inspected["State"]["Status"] == "created"
        assert inspected["State"]["Pid"] == 0 and inspected["State"]["Running"] is False
        assert inspected["State"]["StartedAt"] == "0001-01-01T00:00:00Z"
        assert _original_bytes(preserved) == baseline
