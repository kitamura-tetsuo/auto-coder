# Codex private-workspace repository readiness

Every supervised, Git-backed Codex launch transfers only its invocation-owned
private workspace path to the configured non-root worker. This includes no-edit
launches and private clones made from ordinary or linked-worktree callers; caller
and peer repository ownership and permissions are not changed.

Before prompt delivery, Auto-Coder removes ambient Git target overrides, rejects
Codex directory options that select anything other than the bound private root,
and probes that root after dropping to the configured worker UID, GID, and empty
supplementary-group set. The probe requires exact private-root Git discovery,
private readable Git/common metadata, a resolvable current HEAD, and readable
tracked regular files. It runs anew for every launch and does not compare the
repository with its initial branch, index, or HEAD state.

Failure is reported as private-workspace repository preparation failure with the
workspace and effective numeric worker identity. Codex is not started and its
prompt is not delivered. An explicit `--skip-git-repo-check` does not bypass this
decision, and Auto-Coder does not inject that flag or modify persistent Git/Codex
trust configuration.
