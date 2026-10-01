"""Real Codex/cgroup review driver, run in a fresh controller process."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from auto_coder.invocation_process_supervisor import CgroupV2Owner, InvocationLaunch, InvocationOutcome, InvocationProcessSupervisor
from auto_coder.local_execution_boundary import bind_local_execution_boundary
from auto_coder.pr_processor import TwoTierGateInputs, _execute_pending_strong_audit
from auto_coder.pr_review_cycle import ContractSnapshot, StrongPolicyIdentity
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from auto_coder.worktree_utils import get_current_local_workspace, isolated_local_llm_worktree


def main() -> None:
    version = subprocess.run(["codex", "--version"], check=True, capture_output=True, text=True)
    assert version.stdout.strip() == "codex-cli 0.159.2"
    root = Path(sys.argv[1])
    repository = root / "caller"
    repository.mkdir()
    os.chdir(repository)

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repository / "AGENTS.md").write_text("Distinctive review guidance 2387.\n")
    (repository / "tracked.txt").write_text("Distinctive source base 2387.\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("Distinctive source head 2387.\n")
    git("commit", "-am", "head")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/2387/head", head)
    git("remote", "add", "origin", str(repository))
    # A separate real diagnostic establishes why nested sandboxing is unusable
    # under the same worker credentials and invocation-private write policy.
    diagnostic_runtime = root / "runtime" / "diagnostic"
    diagnostic_home = diagnostic_runtime / "home" / ".codex"
    diagnostic_home.mkdir(parents=True)
    for path in (diagnostic_runtime, diagnostic_home.parent, diagnostic_home):
        os.chown(path, 65532, 65532)
    with isolated_local_llm_worktree(str(repository), is_noedit=True) as workspace:
        binding = get_current_local_workspace()
        assert binding is not None
        for current, directories, files in os.walk(binding.workspace.parent):
            os.chown(current, 65532, 65532)
            for name in (*directories, *files):
                os.chown(Path(current) / name, 65532, 65532, follow_symlinks=False)
        environment = os.environ.copy()
        environment.update(HOME=str(diagnostic_home.parent), CODEX_HOME=str(diagnostic_home), TMPDIR=str(diagnostic_runtime))
        with bind_local_execution_boundary(binding, backend_type="codex", editable=False) as boundary:
            diagnostic = InvocationProcessSupervisor(owner=CgroupV2Owner(worker_uid=65532, worker_gid=65532)).run(
                InvocationLaunch(
                    invocation_id=binding.invocation_id,
                    backend_type="codex",
                    effective_mode="no-edit",
                    result_root=binding.workspace,
                    runtime_paths=(diagnostic_runtime,),
                    executable=shutil.which("codex") or "codex",
                    arguments=("sandbox", "--", "/bin/sh", "-c", "echo NESTED_SANDBOX_STARTED"),
                    cwd=binding.workspace,
                    environment=environment,
                    timeout_seconds=30,
                    protected_paths=(repository,),
                ),
                boundary=boundary,
            )
        assert diagnostic.outcome is InvocationOutcome.FAILED, diagnostic
        assert "NESTED_SANDBOX_STARTED" not in diagnostic.stdout, diagnostic
        print("NESTED_SANDBOX_DIAGNOSTIC:" + diagnostic.stderr)
    (root / "peer").mkdir()
    (root / "peer" / "marker.txt").write_text("peer-protected\n")
    caller_git = (repository / ".git" / "config").read_bytes()
    inputs = TwoTierGateInputs(
        gate=TwoTierPrGate("local/review"),
        contract=ContractSnapshot(("#2387",), "REQ-001: Read the repository under no-edit protection."),
        policy=StrongPolicyIdentity("backend_strong_pr_adversarial_validation", "model=gpt-5-codex", "v1"),
        head_sha=head,
        base_sha=base,
    )
    inputs.gate.ordinary_pass(2387, head, base, inputs.contract)
    ordinary_before = inputs.gate.state.snapshot(2387).ordinary_pass_head_sha
    (root / "head.txt").write_text(head)
    accepted, reason = _execute_pending_strong_audit("local/review", 2387, inputs)
    snapshot = inputs.gate.state.snapshot(2387)
    print(
        "RUNTIME_RESULT:"
        + json.dumps(
            {
                "accepted": accepted,
                "reason": reason,
                "verdict": snapshot.accepted_strong_round.verdict if snapshot.accepted_strong_round else None,
                "ordinary_before": ordinary_before,
                "ordinary_head": snapshot.ordinary_pass_head_sha,
                "head": head,
                "findings": list(snapshot.accepted_strong_round.finding_ids) if snapshot.accepted_strong_round else [],
                "peer": (root / "peer" / "marker.txt").read_text(),
                "caller_git_unchanged": (repository / ".git" / "config").read_bytes() == caller_git,
                "source": (repository / "tracked.txt").read_text(),
            }
        )
    )


if __name__ == "__main__":
    main()
