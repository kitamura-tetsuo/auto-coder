# Muse Code local backend

Meta Muse Code is available as the `muse` backend and as named aliases with
`backend_type = "muse"`. Install and authenticate the `muse` CLI (or provide
`MUSE_API_KEY`) before startup. Auto-Coder invokes `muse exec` once with the
configured model and edit/no-edit options, transporting the complete rendered
task in an execution-scoped private `--prompt-file` outside the repository.
Muse may inspect files, edit the working tree, and run tests, but Auto-Coder
exclusively owns branches, HEAD, staging, commits, pushes, and pull-request
lifecycle operations. A lifecycle mutation, or any repository mutation during
no-edit execution, fails the run. Muse Code is also capable of serving as a
read-only review and adversarial validation backend (`[backend_adversarial_validation]`);
in no-edit mode, Auto-Coder invokes Muse with sandboxed execution (`--disable-write`,
`--disable-shell`, `--disable-approval`) and strips dangerous bypass flags, ensuring
file and command mutations are completely disabled.

Validation and recovery are bound to the worktree and worktree-private Git
directory captured before the invocation. Validation retains the worktree's
symbolic-or-detached HEAD identity, HEAD object, and semantic index entries;
it does not snapshot or compare repository-wide refs. Concurrent branch, tag,
remote-tracking-ref, and peer-worktree registration changes therefore do not
invalidate a clean invocation. Invocation-attributed lifecycle commands still
fail through the per-invocation Git trace, including transient mutations whose
final state matches the initial snapshot. Read-only `git branch --list` and
`git worktree list` inspection is permitted.

Recovery never replays repository-wide refs. HEAD and index recovery uses only
the captured worktree's private state, and an attached HEAD is restored only
while its original branch still points to the captured commit. If that shared
branch has moved or disappeared, recovery refuses the unsafe write and the
invocation remains failed.
