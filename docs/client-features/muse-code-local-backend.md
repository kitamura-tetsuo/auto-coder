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
