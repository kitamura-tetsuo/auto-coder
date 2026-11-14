"""Bounded real-kernel acceptance of the direct entry with no real provider.

Run only inside an approved non-root Docker test environment. This script calls
no Codex executable, inference service, namespace operation or credential store.
It fails rather than bypassing unavailable enforcement prerequisites.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from auto_coder.direct_codex import DirectCodexError, DirectCodexRequest, check_runtime, run_direct_codex
from auto_coder.local_execution_boundary import EvidenceStatus

FIXTURE = r"""#!/bin/sh
IFS= read -r scenario
IFS= read -r editable
IFS= read -r caller
IFS= read -r peer
printf one > "$TMPDIR/started"
IFS= read -r original < source.txt || :
[ "$original" = original ] || exit 10
[ -d "$CODEX_HOME" ] || exit 11
[ -z "${OPENAI_API_KEY+x}${CODEX_API_KEY+x}" ] || exit 12
[ "$scenario" != failure ] || exit 7
if [ "$scenario" = timeout ]; then sleep 10; fi
if [ "$editable" = true ]; then printf 'private edit' > source.txt; fi
denied=0
try_write() {
    if printf forbidden > "$1"; then
        exit 20
    else
        denied=$((denied + 1))
    fi
}
try_write "$caller/source.txt"
try_write "$peer"
if [ "$editable" != true ]; then
    try_write source.txt
    try_write .git/config
fi
printf '{"fixture":"completed","denied":%s,"private_runtime":"one"}\n' "$denied"
"""


def main() -> None:
    check_runtime()
    results: list[dict[str, str | bool | int]] = []
    with tempfile.TemporaryDirectory(prefix="direct-codex-kernel-fixture-") as directory:
        root = Path(directory)
        executable = root / "controlled-provider"
        executable.write_text(FIXTURE)
        executable.chmod(0o700)
        caller = root / "caller"
        caller.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(caller), *args], check=True, capture_output=True, text=True).stdout.strip()

        git("init", "-q")
        git("config", "user.email", "synthetic@example.invalid")
        git("config", "user.name", "Synthetic Test")
        (caller / "source.txt").write_text("original")
        git("add", ".")
        git("commit", "-qm", "synthetic source")
        initial = git("rev-parse", "HEAD")
        peer = root / "peer.txt"
        peer.write_text("peer-original")
        # Both the entry runtime and private-worktree roots use this test-owned
        # directory. No real task roots are listed, read, or cleaned up.
        previous_tmp = tempfile.tempdir
        tempfile.tempdir = str(root)
        try:
            for case, editable in (("success", False), ("success", True), ("failure", True), ("timeout", True)):
                request = DirectCodexRequest(caller, "\n".join((case, "true" if editable else "false", str(caller), str(peer), "")), "controlled-fixture", editable, 0.5 if case == "timeout" else 20)
                if case == "success":
                    result = run_direct_codex(request, executable=executable, provider_environment={})
                    observed = json.loads(result.stdout)
                    assert observed["fixture"] == "completed", observed
                    assert observed["denied"] == (2 if editable else 4), observed
                    assert observed["private_runtime"] == "one", observed
                    assert result.evidence.filesystem_enforcement is EvidenceStatus.ESTABLISHED
                    assert result.evidence.violation_observation is EvidenceStatus.ESTABLISHED
                    assert result.evidence.policy_violation
                    assert result.evidence.direct_process_completion is EvidenceStatus.ESTABLISHED
                    assert result.evidence.writer_completion is EvidenceStatus.UNKNOWN
                    assert result.source_head == initial
                    assert (Path(result.workspace) / "source.txt").read_text() == ("private edit" if editable else "original")
                    results.append({"case": case, "editable": editable, "denied_writes": observed["denied"], "direct_complete": True, "descendant_termination": "unproven"})
                else:
                    try:
                        run_direct_codex(request, executable=executable, provider_environment={})
                    except DirectCodexError as exc:
                        expected = "timed-out" if case == "timeout" else "failed"
                        assert f"supervised invocation failed: {expected};" in str(exc), str(exc)
                        results.append({"case": case, "editable": editable, "successful_handoff": False})
                    else:
                        raise AssertionError("failed execution returned success")
                assert (caller / "source.txt").read_text() == "original"
                assert peer.read_text() == "peer-original"
                assert git("rev-parse", "HEAD") == initial
                assert git("status", "--porcelain") == ""
        finally:
            tempfile.tempdir = previous_tmp
    print("DIRECT_KERNEL_RESULTS:" + json.dumps(results))


if __name__ == "__main__":
    main()
