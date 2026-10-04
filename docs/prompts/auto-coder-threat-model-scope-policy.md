# Auto-Coder Threat Model Scope Policy

This policy is specific to the `kitamura-tetsuo/auto-coder` project.

It governs specification authoring, Issue decomposition, implementation review, and repair when security, isolation, publication authority, credentials, networking, process containment, or sandboxing are involved.

## 1. Core rule

Do not silently strengthen the project's **threat model**, **protected assets**, or **trusted boundaries**.

A reviewer, specification author, or implementation agent must not introduce a stronger adversary, a larger protected-asset set, or a narrower trust boundary merely because doing so would be more secure, more robust, more general, or closer to a hostile-code sandbox.

Security hardening is not automatically a correctness requirement.

If a stronger security property is not required by the fixed Objective, explicit Requirements, or this project policy, it is an adjacent improvement rather than a merge blocker.

## 2. Default Auto-Coder threat model

Unless an Issue explicitly states otherwise, use the following project default:

- Auto-Coder runs inside a controller-selected Docker/container environment.
- The coding agent may make mistakes, run unexpected commands, alter files incorrectly, or leave the working repository in an undesirable Git state.
- The coding agent is **not** treated as a malicious adversary attempting to escape the container, exfiltrate source code, steal credentials, exploit the kernel/runtime, or intentionally bypass every available control path.
- The current primary workload is editing Git-managed open-source repositories.
- Ordinary Docker/container isolation supplied by the deployment environment is a trusted boundary unless the scoped Issue explicitly changes that assumption.

Do not derive additional hostile-agent assumptions merely from the fact that the agent can execute shell commands or arbitrary repository code.

## 3. Default protected assets

Unless an Issue explicitly expands the set, Auto-Coder is responsible for protecting correctness-critical local execution state, including:

- the authoritative caller/controller state that must not be accidentally overwritten by an invocation;
- workspaces belonging to other concurrent invocations;
- result ownership and synchronization boundaries required to determine which edits belong to which invocation;
- data whose accidental corruption would make Auto-Coder apply, merge, or attribute the wrong implementation result.

The following are **not protected assets by default** merely because a stronger sandbox could protect them:

- secrecy of source code from arbitrary Internet exfiltration;
- GitHub repositories from every possible agent-originated publication path;
- GitHub Issues, PRs, comments, reviews, or merge APIs from a deliberately malicious agent;
- host security against a container-runtime or kernel escape;
- credentials that the deployment intentionally exposes to the container under its own trust model;
- arbitrary external services reachable from the container.

Adding any of these as protected assets is a threat-model change and requires explicit scope authority.

## 4. Default trusted boundaries

Unless explicitly changed by an Issue:

- Docker/container isolation is trusted for host/container separation.
- Controller-selected mounts, workspace placement, and container lifecycle are trusted configuration boundaries.
- Git is trusted as the normal mechanism for observing and recovering tracked repository changes.
- A private or invocation-owned working repository may be used as an accident-containment boundary without implying hostile-code confinement.
- Existing deployment networking is trusted according to its configured policy; Auto-Coder does not automatically own Internet egress control.
- Provider CLIs and configured model-inference transports are treated according to their supported operational contract, not as hostile processes that must be mediated at every syscall or HTTP request.

Do not replace these trusted boundaries with stricter application-level enforcement unless the Issue explicitly requires that change.

## 5. Accident containment is different from adversarial confinement

Specifications must distinguish these two goals.

**Accident containment** may require behavior such as:

- using a separate working clone/worktree/container;
- avoiding writable mounts of unrelated repositories;
- preventing one invocation from overwriting another invocation's owned state;
- making cleanup deterministic enough to avoid stale work affecting later work;
- preserving Git-visible recovery of intended repository edits.

**Adversarial confinement** includes stronger properties such as:

- preventing every possible Git/GitHub publication mechanism;
- preventing direct HTTP publication through arbitrary interpreters;
- preventing source-code exfiltration to arbitrary Internet destinations;
- preventing credential use through files, helpers, sockets, or alternate clients;
- enforcing inference allowlists by HTTP method/path/redirect semantics;
- defending against container, namespace, runtime, or kernel escape;
- treating every descendant process as malicious and independently escape-seeking.

Do not turn an accident-containment requirement into adversarial confinement without explicit authorization.

## 6. No implicit security escalation during preflight

During Issue preflight, decomposition, or specification repair, do not add a Requirement merely because an implementation could otherwise be less secure under a stronger hypothetical adversary.

Before adding a security/isolation Requirement, identify:

1. the concrete protected asset;
2. the concrete actor or failure mode that threatens it;
3. the currently trusted boundary that is insufficient;
4. the observable project-level consequence if the Requirement is omitted;
5. the source of authority for treating that consequence as in scope.

If any of these cannot be identified from the fixed Objective, explicit Requirements, this policy, or an explicit user decision, do not make the stronger property merge-blocking.

## 7. No implicit security escalation during review

Reviewers must not convert an implementation into a hostile-code sandbox review unless the Issue explicitly requires hostile-code confinement.

The following arguments are insufficient by themselves to create a blocker:

- "An arbitrary script could bypass this."
- "A malicious agent could use an absolute executable."
- "A direct HTTP client could avoid the wrapper."
- "A credential could exist somewhere else."
- "A descendant could intentionally evade the intended workflow."
- "The container could theoretically be escaped."
- "A more privileged or kernel-enforced mechanism would be safer."

Such observations become blockers only when the scoped contract already requires protection against that adversary or when the demonstrated path violates an explicitly protected correctness boundary.

A review may still identify accidental cross-workspace corruption, wrong-result attribution, stale lifecycle ownership, or another in-scope correctness failure even if the same mechanism could also be described as a security boundary.

## 8. No implicit security escalation during repair

An implementation agent responding to review must not silently accept a stronger threat model introduced by reviewer prose.

Classify a security-related finding as one of:

- **In-scope correctness defect:** violates an existing protected asset or trusted-boundary contract.
- **In-scope clarification:** closes an ambiguity already required by the Objective/Requirements.
- **Threat-model change:** adds a new adversary, protected asset, or distrust assumption.
- **Adjacent hardening:** improves defense without being required for correctness.

For a threat-model change, stop automatic scope expansion and request an explicit user decision or create a separately authorized follow-up specification.

## 9. Publication and network behavior

Unless explicitly required by an Issue, Auto-Coder does not promise to prevent an agent from:

- pushing to a Git remote;
- invoking GitHub mutation APIs;
- making arbitrary outbound network requests;
- sending repository contents to an Internet endpoint;
- using credentials intentionally available inside the container.

If publication authority or network secrecy must be restricted in the future, specify that as a dedicated threat-model change with its own protected assets, trusted boundaries, supported runtime profile, and authoritative test oracle.

Do not infer such a requirement merely from the existence of GitHub credentials, network access, or executable agent code.

## 10. Container security

Unless explicitly required by an Issue, Auto-Coder does not implement its own defense against malicious container breakout or kernel/runtime exploitation.

Do not require Auto-Coder application code to reproduce Docker/container-runtime responsibilities through nested namespaces, syscall mediation, seccomp-like enforcement, custom egress brokers, kernel policy, or equivalent sandbox infrastructure solely for defense in depth.

Such mechanisms may be introduced only when they directly satisfy an explicitly approved project requirement.

## 11. Git and workspace expectations

Git-managed workspaces remain an important correctness boundary even under the relaxed threat model.

Specifications may require:

- an invocation-owned clone/worktree or equivalent isolated working state;
- deterministic identification of the implementation result;
- preservation of unrelated caller/peer working state;
- safe synchronization/application of intended edits;
- cleanup sufficient to prevent stale invocation state from being mistaken for current work.

These requirements protect Auto-Coder correctness and recoverability. They do not imply that the workspace must resist a deliberately malicious process with arbitrary code execution.

## 12. When stronger hardening is justified

A stronger threat model may be introduced when the user explicitly chooses it or when a new project use case makes it necessary, for example:

- private or confidential source repositories whose network exfiltration must be prevented;
- multi-tenant execution with mutually untrusted users;
- untrusted third-party code where container escape is within scope;
- publication credentials that must be available to the same agent runtime while publication itself must still be prevented;
- compliance requirements mandating egress or syscall restrictions.

When this happens, do not silently retrofit the stronger assumptions into unrelated existing Issues. Perform a fresh preflight and identify the new protected assets, adversary, trusted boundaries, implementation stages, runtime capabilities, and test oracles explicitly.

## 13. Project-specific preflight check

Before creating or materially strengthening any Issue involving sandboxing, isolation, credentials, publication, or networking, answer:

- What is the protected asset under the current Auto-Coder threat model?
- Is the concern accidental misuse or malicious bypass?
- Which existing boundary is currently trusted?
- Does the requested Objective explicitly require distrusting that boundary?
- Would the proposed Requirement defend against a real current Auto-Coder failure, or only against a hypothetical stronger adversary?
- Can the intended correctness property be achieved with the existing Docker/Git/workspace boundaries instead of creating a new sandbox subsystem?

If the only justification is defense against a stronger unrequested adversary, exclude it from the current merge-blocking contract.

## 14. Interaction with the Issue decomposition preflight

This policy constrains the Auto-Coder Issue decomposition preflight rather than replacing it.

The preflight must still identify real correctness responsibilities, authoritative producers, false-success paths, missing lifecycle semantics, and independently testable capability boundaries.

However, it must do so **inside the threat model, protected-asset set, and trusted boundaries defined here and by the user's explicit request**.

The mandatory split gate must not manufacture new security capabilities merely because they would make the system more generally hardened.

A missing producer for an unrequested security property is not a missing prerequisite; it is out of scope unless the threat model is explicitly changed.

## 15. Review stopping rule

Once all demonstrated defects within the approved threat model and explicit contract are addressed, stop.

Do not continue escalating from:

`workspace correctness` → `publication prevention` → `network exfiltration prevention` → `hostile container confinement` → `kernel/runtime defense`

unless each transition is explicitly authorized by the user or by an already-established project requirement.
