# Git-state repository hygiene inspection

The standalone `scripts/check_repository_hygiene.py` checker validates either the
complete Git index (the default) or the checked-out HEAD tree.  It reads the explicit
root-file allowlist from that same selected Git state, rejects every tracked root file
outside the boundary, always rejects root-level Python and `.agent-tmp` paths, and
ignores untracked or working-tree-only artifacts.  Invalid policies, conflicted index
states, and unavailable Git states fail closed without modifying the repository.
