# Muse Code local backend

Meta Muse Code is available as the `muse` backend and as named aliases with
`backend_type = "muse"`. Install and authenticate the `muse` CLI (or provide
`MUSE_API_KEY`) before startup. Auto-Coder starts a separately owned
`muse serve` MSP host for each invocation and submits the complete rendered
task as one typed text part over the protocol.
Muse may inspect files, edit the working tree, run tests, and use local Git
operations inside its controller-owned private repository. Staging, commits,
branches, refs, stashes, merges, rebases, cherry-picks, and private subagent
worktrees are retained as implementation state; Auto-Coder still owns result
handoff and pull-request publication. Any repository mutation during no-edit
execution fails the run. Muse Code is also capable of serving as a
read-only review and adversarial validation backend (`[backend_adversarial_validation]`);
in no-edit mode, Auto-Coder starts Muse with `--disable-write` and
`--disable-shell`, then establishes `denyUnmatched` approval mode over MSP
before submitting the turn. Dangerous bypass and unauthorized workspace-trust
options fail closed rather than being stripped or passed to the host.

Every editable session also starts with confirmed `denyUnmatched` approval mode.
Exact-session resume establishes that mode before submitting a new turn. Required
operations must already be allowed by the operator's Muse permission rules;
Auto-Coder does not invent policy rules or grant blanket approval. Unmatched
operations receive Muse's model-visible denial instead of waiting for a person.
This approval policy does not enable sandbox network access. Dependencies should
be prepared by the controller's initial `scripts/test.sh` execution or by the
target repository's setup procedure before invoking the provider.

An unexpected `approval/requested`, `approval/updated`, or `userInput/requested`
frame fails promptly, including before the turn acknowledgement. Other server
requests receive an unsupported-request response and fail the invocation. No
request is automatically approved. The `llm.muse-interactive-request` stage
reports a blocked outcome and bounded correlation identifiers to the dashboard,
without copying shell commands, prompt text, or approval subjects. The owned host
and its process group are settled by the existing failure path.

No-edit validation and recovery remain bound to the captured worktree and
worktree-private Git directory. Its snapshot and Git trace detect both final and
transient mutation, and recovery never replays unrelated or newer refs. Editable
turns intentionally do not run this Git-mutation rejection or snapshot recovery;
their exact current generation is instead validated and delivered through the
shared local-result handoff.
