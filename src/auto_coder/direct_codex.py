"""One-shot operator entry for supervised Codex, without caller publication.

Not installed automatically: callers must authorize the channel, execution mode,
and filesystem scope before invoking this entry. It never configures Docker or
copies credentials from the user's home.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .invocation_process_supervisor import InvocationLaunch, InvocationOutcome, InvocationProcessSupervisor, PromptTransport
from .local_execution_boundary import LocalBoundaryEvidence, bind_local_execution_boundary
from .repository_readiness import verify_worker_repository
from .worktree_utils import get_current_local_workspace, is_git_repository, isolated_local_llm_worktree


class DirectCodexError(RuntimeError):
    """The operator request could not produce a supervised result."""


@dataclass(frozen=True)
class DirectCodexRequest:
    repository: Path
    prompt: str
    model: str
    editable: bool = False
    timeout_seconds: float = 600


@dataclass(frozen=True)
class DirectCodexResult:
    invocation_id: str
    source_head: str
    source_checkpoint: str
    stdout: str
    stderr: str
    evidence: LocalBoundaryEvidence
    # No provider files are copied to the caller by this entry.
    workspace: str
    runtime: str


def codex_arguments(request: DirectCodexRequest) -> tuple[str, ...]:
    if not request.prompt.strip() or not request.model.strip() or not math.isfinite(request.timeout_seconds) or request.timeout_seconds <= 0:
        raise DirectCodexError("prompt, model and positive timeout are required")
    # This argument is valid only inside the mandatory supervisor below. Never
    # call it through bare Popen, or retry without policy installation.
    return ("--sandbox", "danger-full-access", "--ask-for-approval", "never", "-c", 'approvals_reviewer="user"', "exec", "--model", request.model, "-")


def private_environment(home: Path, temporary: Path, supplied: Mapping[str, str]) -> dict[str, str]:
    """Allow only explicit provider credentials; never copy ambient auth/config."""
    allowed = {"OPENAI_API_KEY", "CODEX_API_KEY"}
    unknown = set(supplied) - allowed
    if unknown:
        raise DirectCodexError("unsupported environment keys: " + ", ".join(sorted(unknown)))
    return {
        "PATH": os.defpath,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "TMPDIR": str(temporary),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        **supplied,
    }


def selected_credentials(names: Sequence[str], environment: Mapping[str, str]) -> dict[str, str]:
    """Read only operator-selected names; the default never reads a secret."""
    if any(name not in {"OPENAI_API_KEY", "CODEX_API_KEY"} for name in names):
        raise DirectCodexError("unsupported credential name")
    try:
        return {name: environment[name] for name in names}
    except KeyError as exc:
        raise DirectCodexError("the selected credential is unavailable") from exc


def check_runtime() -> None:
    """Refuse missing baseline conditions, without changing host/container policy.

    These are local checks, not authoritative attestation of Docker mounts or
    namespace ownership. The operator must separately review the container profile.
    """
    if os.geteuid() == 0:
        raise DirectCodexError("a non-root controller is required")
    if not (Path("/.dockerenv").is_file() or Path("/run/.containerenv").is_file()):
        raise DirectCodexError("the approved container boundary is unavailable")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if int(status.get("CapEff", "1").strip(), 16) != 0 or status.get("NoNewPrivs", "0").strip() != "1":
        raise DirectCodexError("zero effective capabilities and no-new-privileges are required")
    if status.get("Seccomp", "0").strip() != "2":
        raise DirectCodexError("the existing seccomp filter must remain enabled")
    if any(Path(path).exists() for path in ("/var/run/docker.sock", "/run/docker.sock")):
        raise DirectCodexError("a Docker control socket must not be exposed")


def run_direct_codex(request: DirectCodexRequest, *, executable: Path, provider_environment: Mapping[str, str]) -> DirectCodexResult:
    """Run in a fresh private root; preserve it as an unvalidated result artifact.

    No continuation, GitHub action, deployment, caller writeback, or automatic
    cleanup of the retained result is performed. Direct child completion does
    not certify all-descendant termination. Retained files may still change;
    this result is explicitly NOT a validated immutable publication artifact.
    """
    arguments = codex_arguments(request)
    check_runtime()
    repository = request.repository.resolve(strict=True)
    executable = executable.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DirectCodexError("Codex executable is not available")
    if os.geteuid() == 0 or not is_git_repository(repository):
        raise DirectCodexError("a non-root controller and Git repository are required")
    # All temporary state is outside the caller checkout. Creating this root
    # does not alter mounts or add privileges.
    parent = Path(tempfile.mkdtemp(prefix="auto-coder-direct-"))
    home = parent / "home"
    (home / ".codex").mkdir(parents=True)
    temporary = parent / "tmp"
    temporary.mkdir()
    started = False
    binding = None
    try:
        environment = private_environment(home, temporary, provider_environment)
        # No writeback: edit permission belongs only to the invocation boundary.
        # The private-worktree context itself is deliberately read-only to caller.
        with isolated_local_llm_worktree(repository, is_noedit=True) as workspace:
            binding = get_current_local_workspace()
            if binding is None:
                raise DirectCodexError("private workspace binding is unavailable")
            binding.ownership.retain_session()
            verify_worker_repository(binding.workspace, worker_uid=os.geteuid(), worker_gid=os.getegid(), environment=environment)
            with bind_local_execution_boundary(binding, backend_type="codex", editable=request.editable) as boundary:
                launch = InvocationLaunch(
                    invocation_id=binding.invocation_id,
                    backend_type="codex",
                    effective_mode="editable" if request.editable else "no-edit",
                    result_root=binding.workspace,
                    runtime_paths=(home, temporary),
                    executable=str(executable),
                    arguments=arguments,
                    prompt_transport=PromptTransport.STDIN,
                    prompt=request.prompt,
                    timeout_seconds=request.timeout_seconds,
                    cwd=binding.workspace,
                    environment=environment,
                    protected_paths=(binding.caller_root,),
                )
                started = True
                result = InvocationProcessSupervisor().run(launch, boundary=boundary)
                evidence = boundary.evidence()
                if result.outcome is not InvocationOutcome.SUCCEEDED or not evidence.confined_result_authorized:
                    raise DirectCodexError(f"supervised invocation failed: {result.outcome.value}; {result.detail}; retained workspace={workspace}; runtime={parent}")
                return DirectCodexResult(binding.invocation_id, binding.initial_commit, binding.file_snapshot_checksum, result.stdout, result.stderr, evidence, workspace, str(parent))
    finally:
        if not started:
            if binding is not None:
                binding.ownership.release_session()
            shutil.rmtree(parent)
        # After attempted execution, do not claim cleanup or descendant proof.
        # An operator owns retained result disposal, not a later reused session.


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot supervised Codex; no checkout publication or Docker configuration")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--editable", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--credential-env", choices=("OPENAI_API_KEY", "CODEX_API_KEY"), action="append", default=[])
    args = parser.parse_args()
    request = DirectCodexRequest(args.repository, args.prompt_file.read_text(), args.model, args.editable, args.timeout)
    # Environment forwarding is explicit and narrowly named. No login state is
    # discovered, read, copied, or modified by this entry.
    provider = selected_credentials(args.credential_env, os.environ)
    result = run_direct_codex(request, executable=args.executable, provider_environment=provider)
    print(
        json.dumps(
            {
                "invocation_id": result.invocation_id,
                "source_head": result.source_head,
                "source_checkpoint": result.source_checkpoint,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "workspace": result.workspace,
                "runtime": result.runtime,
                "descendant_termination": "unproven",
                "publication": "not_performed",
            }
        )
    )


if __name__ == "__main__":
    main()
