# Auto-Coder Issue Decomposition Preflight Prompt

Use this prompt before creating any non-trivial GitHub Issue.

---

## Prompt

Before creating any Issue, perform an adversarial **code-and-contract preflight**.

The goal is not to turn my initial request directly into an Issue.
The goal is to discover the smallest independently correct implementation stages, their ordering, and the authoritative behavior each stage must preserve or change.

### 0. Establish a short, fixed Objective

Use **one Issue, one Objective, at most two short sentences**.

Before investigating implementation details, write a provisional objective from the user's request. After the code-and-contract preflight determines the stage boundaries, give the parent and each child its own Objective and show those Objectives in the proposed decomposition before creation.

For each Objective:

- Use one or two short prose sentences, not a list or a compressed Requirements section.
- State the desired outcome or change. Use the second sentence only when an essential preserved property or scope boundary needs to be explicit.
- Do not enumerate implementation steps, internal mechanisms, test methods, hypothetical variants, or adjacent improvements. Name a mechanism only when changing that mechanism is itself the user's requested outcome.
- Do not broaden the user's request to accommodate a preferred solution. If two materially different goals remain plausible, stop and ask the user before creating the Issue.
- If the Objective cannot remain short without combining independent outcomes, reconsider the decomposition. Do not hide multiple objectives in a long sentence.

Once the Issue is created, its Objective is a fixed **scope anchor** throughout automated review and repair. Neither the reviewer nor the responding/authoring agent may rewrite, expand, weaken, replace, or delete it. An explicit change of purpose requires a user decision and a fresh preflight, not silent acceptance of a reviewer's suggestion. Preserve the original Objective separately from mutable review context when handing work to another agent; re-summarizing the latest Requirements is not a way to recover the original Objective.

The Objective is **not a second implementation contract**. It bounds specification authoring and review; `Requirements` alone define merge-blocking implementation behavior. An Objective/Requirements mismatch is a specification defect to clarify explicitly in Requirements, not permission for an implementation or PR reviewer to invent an unstated obligation.

A short Objective is deliberately not exhaustive. Do not delete an explicit Requirement or ignore a material contradiction merely because the Objective does not repeat its details. Do not freeze an AI guess as confirmed user intent.

One user-visible outcome is not necessarily one implementation responsibility. A short Objective may require several independently missing runtime capabilities or authoritative boundaries. Do not use Objective unity, a shared feature name, or a common module as evidence that those capabilities belong in one implementation Issue.

Example:

> Let users preview a saved draft without applying it. Leave the saved draft and live configuration unchanged.

### 1. Inspect production behavior first

Search the relevant production code and regression tests broadly before proposing Issue structure. Use the requested outcome to bound that investigation: the checklist below is a set of inspection lenses, not a list of behaviors that every Issue must acquire.

For the requested change, identify every place where the current mechanism is used as any of the following:

- authority or gate;
- lock / exclusion mechanism;
- lifecycle state;
- persistence or restart-recovery state;
- retry/requeue trigger;
- wake-up / invalidation source;
- deduplication mechanism;
- provider/session ownership evidence;
- CI/workflow coordination;
- prompt or semantic input;
- UI/presentation only;
- compatibility/configuration surface;
- cleanup/recovery side effect.

Do not assume two uses with the same name have the same responsibility.

Explicitly search for:
- direct reads;
- direct writes;
- helper abstractions;
- stale-state workarounds;
- restart paths;
- background/asynchronous paths;
- failure handlers;
- provider-specific paths;
- CLI/configuration flags;
- tests that encode historical behavior;
- comments/documentation that reveal why a workaround exists.

### 2. Distinguish current semantics from implementation artifacts

For each discovered use, classify it as one of:

1. **Behavior that must be preserved**
2. **Behavior intentionally being removed**
3. **Implementation artifact that must disappear**
4. **Correctness responsibility that needs a replacement before removal**
5. **Unrelated behavior that must remain untouched**

Existing code is evidence, not automatically the desired specification.

Do not copy incidental implementation details into new Requirements merely to preserve “existing behavior”.

If removing one mechanism exposes an independent correctness responsibility, split that responsibility into its own Issue instead of expanding the removal Issue.

### 3. Search for false-success removals

Before proposing Issues, answer:

> If an implementation simply deletes the obvious code for the requested mechanism, what could silently break while the common path still appears to work?

Specifically examine:

- restart between state transition and observation;
- concurrent workers;
- external operation accepted but response lost;
- state written before external operation, then crash;
- external operation sent before local state is persisted;
- temporary absence of external evidence;
- old evidence becoming observable later;
- retry/reissue paths;
- stale PR/session cleanup;
- provider task still running after local execution returns;
- webhook/self-generated-event feedback;
- prompt inputs and semantic normalization;
- configuration aliases;
- helper-level tests that bypass the real production origin.

### 4. Decompose by semantic responsibility

Prefer multiple small Issues when correctness boundaries differ. Treat a semantic responsibility as an independently implementable capability or policy boundary with its own authoritative producer, failure state and test oracle, not merely as a broad feature name such as "execution boundary", "synchronization", "authorization", "lifecycle" or "integration".

Give each proposed child its own short Objective. A tracking parent's Objective describes completion of the staged change, not an additional implementation task. Children do not inherit either the parent's Objective or another child's Requirements as hidden implementation obligations.

A necessary prerequisite can justify another stage only when its necessity follows from the requested outcome or an explicit preservation obligation. An adjacent improvement is not automatically a prerequisite. Optional architectural cleanup remains outside the required set unless the user requested it; do not require it for completion merely because preflight discovered it.

Typical decomposition order:

1. **Prerequisite replacement / safety mechanism**
   - Example: durable idempotency replacing a label-based lock around an external operation.

2. **Input non-interference / compatibility isolation**
   - Example: make a retired marker semantically inert in prompts, semantic resolution, or webhook invalidation without redefining unrelated label behavior.

3. **Lifecycle removal**
   - Remove the old authority/gate/read/write/configuration only after required replacements exist.

4. **Follow-up architecture cleanup**
   - Example: replace polling with webhook-driven lifecycle, or centralize rate-limit handling.

Do not combine:
- external-operation idempotency with broad lifecycle removal;
- ownership-model changes with removal of an old lock;
- semantic resolver redesign with filtering one retired input;
- polling removal with a correctness prerequisite unless they are inseparable.

### 4a. Inventory required capabilities and apply a mandatory split gate

Before finalizing the proposed Issue structure, build a compact **capability inventory** for every proposed child. Keep this inventory in preflight evidence rather than turning it into a second normative contract.

For each capability or correctness responsibility record:

- the capability/responsibility name;
- the current authoritative producer in production code, or `none`;
- the authoritative observation/evidence it produces;
- the consumer that makes a success, authorization, settlement, refusal or promotion decision from that evidence;
- its explicit failure/unavailable/unknown state;
- whether it can fail independently of the other listed capabilities;
- the supported production origin and test harness needed to verify it;
- the REQ IDs that consume or define it;
- the proposed owning Issue.

A capability is a runtime mechanism or semantic authority that can be implemented and tested independently, for example process-lifetime ownership, filesystem confinement, publication/network mediation, durable idempotency, session ownership, a controller-owned evidence model or an external reconciliation mechanism. A helper function, class name, module or broad feature label is not by itself a capability boundary.

Apply these split rules as hard preflight gates:

1. **Multiple absent independent capabilities require decomposition.** If one proposed child requires creating two or more capabilities that do not currently exist and those capabilities have independently observable failure modes, independently meaningful consumers, or materially different authoritative/test boundaries, split them into prerequisite Issues unless they are demonstrably atomic.
2. **Atomicity must be demonstrated, not assumed.** Two missing capabilities may remain in one child only when neither can be implemented, consumed or meaningfully verified without the other and separating them would create an artificial interface with no independently correct state. Sharing one Objective, process, module or eventual integration point is not evidence of atomicity.
3. **Producer and integration are different responsibilities.** When an aggregate boundary consumes several independently produced facts, put the missing producers in prerequisite Issues and make the aggregate composition/adoption a later Issue. Do not make one "boundary" Issue invent every producer merely because they are all needed for the same final decision.
4. **Different control planes are presumptively separate.** Process/descendant lifetime, filesystem authority, network/publication authority, credential mediation, persistence/restart state, external reconciliation and provider-specific adaptation are separate correctness boundaries by default. Combine them only with an explicit atomicity justification grounded in the current production architecture.
5. **Test-harness divergence is a split signal.** If proving separate Requirements needs materially different authoritative harnesses or controlled boundaries, such as a live detached descendant, an actual protected filesystem write, and a captured external publication endpoint, presume separate capabilities and reconsider the decomposition.
6. **A short Objective does not waive the split gate.** One user-visible outcome may need several implementation stages. Keep the same parent outcome while assigning each independently missing capability its own child Objective.
7. **`difficult` is not a substitute for decomposition.** Difficulty caused by combining several missing architectural capabilities is evidence that the Issue is too broad. Use `difficult` only after decomposition, when one remaining correctness boundary is intrinsically difficult.
8. **Do not over-split an indivisible mechanism.** Multiple state transitions, negative cases or test variants of one authoritative mechanism may stay together when they share one producer, one authority model and one independently meaningful completion boundary.

If this gate requires decomposition, repeat the capability inventory after splitting and verify that each child now owns a bounded capability or a bounded integration responsibility.

### 4b. Audit authoritative producer provenance before writing Requirements

For every proposed Requirement predicate that decides whether work is allowed, successful, settled, confined, compatible, durable, current, owned, retryable, promotable or safe to release, identify the authoritative producer of that fact before finalizing the Requirement.

Record a compact provenance tuple in preflight evidence:

`predicate / producer / existing-or-new / authoritative evidence / consumer / failure-or-unknown state / production test origin`

Apply these rules:

- If the producer does not exist, write `producer: none`; do not treat a consumer-side Boolean, default value, model claim, parent return, empty error list, path convention or helper name as if it produced the missing fact.
- If `producer: none` appears for several independently testable predicates, treat those as candidate prerequisite capabilities and re-run the mandatory split gate in 4a.
- A child may legitimately own one new producer plus its local state machine and tests. The problem is not that a mechanism is new; the problem is hiding several independent missing mechanisms behind one aggregate Requirement set.
- If a provider-specific producer already exists but the requested semantics are provider-independent, distinguish "adapt/integrate existing producer" from "create new shared producer". Do not let one provider accidentally define the shared contract.
- If the producer is external or platform-specific, identify how support/unavailability is determined before task submission and what positive runtime profile proves that the capability is usable.
- If no authoritative producer can exist under the stated platform or constraints, stop and ask the user rather than weakening the Requirement or fabricating evidence.

Requirements may consume semantics from a prerequisite only when the child copies the minimal consumed semantics into its own Requirements and declares the dependency structurally. `Blocked-By:` is implementation ordering, not hidden producer semantics.

### 4c. Run a `CANNOT_FIX` pre-mortem before publishing the decomposition

Before Issue creation, imagine a competent implementation agent receiving each proposed child against the repository exactly as it exists now. The agent is not allowed to weaken the contract, invent test-only authority, or pretend a missing runtime capability exists.

For each child answer:

- Which Requirements, if any, could the agent truthfully answer `CANNOT_FIX` for because a required prerequisite capability does not yet exist?
- What missing mechanism would force that answer?
- Would satisfying the Issue tempt an implementation to fabricate evidence, add a helper Boolean, rely on post-hoc restoration, or disable the whole feature instead of creating the required authority?
- Can that missing mechanism be implemented and verified independently before the consumer/integration work?
- Is the reason truly an external impossibility, or is it simply a capability that this decomposition failed to give its own implementation stage?

Apply this gate:

- Two or more independent `CANNOT_FIX` causes in one child normally require splitting that child.
- One `CANNOT_FIX` cause that itself spans multiple independent producers/control planes also requires re-running 4a rather than creating one giant prerequisite.
- If `CANNOT_FIX` would say "the current architecture does not provide X" and X is itself required by the requested outcome, absence of X is not a reason to weaken the contract; create an implementation stage that owns X.
- If the problem is only that the implementation technique is uncertain while the capability and oracle are well-bounded, keep the Issue and consider `difficult`. Do not split merely because several implementation approaches are possible.
- If the blocker is a genuine external/platform limitation that prevents the requested behavior, surface the decision to the user before creating Issues.

The purpose of this pre-mortem is to discover missing prerequisites before an implementation/review loop gets stuck between `NEEDS_FIX` and `CANNOT_FIX`.

### 5. Minimize normative surface

For behavior that should remain unchanged, prefer a **non-interference invariant** over restating the whole existing subsystem.

Example pattern:

> For two authoritative states identical except for the retired input X, Auto-Coder must produce identical output Y.

Use this instead of re-specifying every matching, priority, retry, ownership, or provider rule unless that rule itself is changing.

Do not turn a narrow Issue into the normative specification for an entire subsystem.

For each proposed new or strengthened Requirement, explain which demonstrated gap it closes within the fixed Objective and the explicit contract. Prefer an observable restriction over mandating a particular technique when multiple implementations are valid.

Use this counterexample test:

> Without this restriction, could a plausible implementation satisfy the remaining contract while failing the stated outcome or an explicit required invariant?

Do not use the weaker test "Does any correct implementation exist without this Requirement?" The existence of one correct implementation does not exclude incorrect implementations admitted by an underspecified contract.

Do not turn a review comment, prior AI summary, suggested solution, or accepted wording from a previous round into an independent source of purpose. Trace disputed scope back to the fixed Objective and the explicit Requirements.

### 5a. Close each child contract over operation variants and boundary inputs

Before finalizing a child, build a compact applicability table for the operations whose differences affect this change. Record each operation/payload variant, its authoritative inputs, allowed effects, refusal conditions, and owning REQ IDs. Keep the table in preflight evidence, not inside `Requirements` and not as a second contract.

Check overlapping rows as well as missing rows. An unqualified rule such as “every structural paste creates new objects” also covers Cut/Paste unless its domain explicitly excludes it; a nearby Copy example does not narrow that rule. State variant restrictions in the applicable REQ itself, and check that every supported variant has a mutually consistent outcome.

For predicates that decide success or refusal, such as “writable”, “supported”, “valid destination”, or “recoverable”, identify enough authoritative semantics to decide the outcome from the child alone. Where relevant, distinguish project/server capability from surface editability; read permission from mutation permission; source-side effects from destination-side effects; and initiation-time checks from checks at the actual mutation boundary. State how relevant states arise at a supported production boundary without inventing new roles, platforms, or access modes merely for a test.

When another stage supplies these inputs, copy the minimal consumed semantics into the child's Requirements and record their origin if useful. A `Blocked-By:` edge establishes implementation order, not inherited Requirements. Keep provisioning the capability/placement model in its owning prerequisite; do not turn each consumer into an authentication or tree-model redesign. A provider implementation, sibling title, helper name, or mutable “existing behavior” reference is not a local oracle. If the necessary authoritative input is not actually supplied, record the missing prerequisite rather than pretending a test-only Boolean establishes it.

### 5b. Define delayed authority and conflicting preservation obligations

Apply this check only when the scoped feature already holds a pending operation, retry token, clipboard transfer, history entry, or other authority to act on state later.

Write a small transition table covering creation, pending state, success/consumption, refusal/recovery, invalidation, and replay where those transitions matter. Identify what authorizes the later effect, when the first destructive change occurs, what remains recoverable after failure, and whether retry means continuing the same operation or creating a new one. State whether Undo/Redo reverses document effects only or also changes an operation's consumed status. Define session/restart behavior only where needed to prevent an in-scope stale operation from regaining authority; do not assume persistent global coordination is required.

Exercise intervention between capture and execution: an affected object/occurrence is moved or deleted, an ancestor changes, authority is revoked, or a required destination disappears. Distinguish these from content-only changes and temporary missing evidence; specify which relevant changes invalidate the operation and which must survive without invalidating it. Reject an old-state/new-authority combination that can resurrect a deleted placement or overwrite newer accepted state unless that behavior was explicitly chosen.

When Requirements both reverse a local action and preserve another actor's accepted changes, construct a case where those obligations intersect. Examples include a peer adding a child beneath a locally pasted subtree and a peer moving an occurrence that local Undo would remove. Define the observable conflict policy, its atomicity, the validation boundary for both Undo and Redo, and what a refusal does to the pending operation or history position. “Preserve remote changes” alone does not choose between refusal, rebasing, and partial application. Do not mandate one of those policies merely because it is easiest to implement.

Put the selected semantics in Requirements and derive tests from them. If materially different user-visible policies remain possible and the user has not selected or delegated that choice, present the alternatives and ask before publishing a policy as authoritative. If completing these tables reveals independent correctness boundaries, revisit decomposition instead of continuously enlarging one Issue.

### 6. Build the dependency graph before creating Issues

Auto-Coder's implementation ordering is controlled through explicit sibling dependency declarations. Parent relationships define the family; Issue numbers and creation order do not imply implementation precedence.

Therefore, before creating Issues:

- determine which stages have real implementation dependencies;
- create a tracking-only parent when multiple staged children belong to one coordinated decomposition;
- put children that participate in the same dependency graph under the same direct parent;
- add `Parent-Issue: #<parent>` to every generated child body;
- add exactly one `Blocked-By:` declaration to every generated child body, outside `## Requirements`;
- leave `Blocked-By:` empty for a dependency root;
- otherwise list the child's complete intended direct prerequisite set as comma-separated local Issue references, for example `Blocked-By: #123, #456`;
- reference only distinct direct sibling Issues in `Blocked-By:`; do not reference the child itself, the parent, unrelated Issues, or pull requests;
- reject cyclic dependency graphs before creation;
- never use Issue numbers, child creation order, or prose such as “depends on #123” as a substitute for `Blocked-By:`;
- create dependency roots and prerequisites before their dependents so every child's initial body can contain its final `Blocked-By:` references at creation time;
- ensure a dependent child cannot become implementation-eligible while any declared open prerequisite remains unsatisfied.

Treat `Blocked-By:` as structural metadata, not as a merge-blocking Requirement. An explicitly empty declaration means the child has no declared incoming sibling dependency; omission is not equivalent and must not be used for generated children.

The tracking parent must not become another implementation contract.  
Its purpose is coordination/completion only; implementation ordering belongs to the children's explicit dependency edges.

### 7. Review the proposed Issue graph adversarially

Before creating anything, check:

- Could a dependent child be implemented before a declared prerequisite?
- Does every generated child have exactly one `Blocked-By:` declaration, including an empty declaration for dependency roots?
- Does each `Blocked-By:` declaration exactly represent the intended direct sibling dependency edges rather than relying on Issue number or creation order?
- Could an invalid dependency edge target the parent, a non-sibling, the child itself, a pull request, or participate in a cycle?
- Could the parent itself accidentally be treated as an implementation target?
- Does any child rely on unstated semantics from another child?
- Did a child inherit unrelated behavior merely because current code shares a helper/module?
- Could an incorrect implementation satisfy each child in isolation while the composed result is wrong?
- Is one child still spanning multiple independent correctness boundaries?
- Does any Issue redefine existing ownership/retry/provider semantics unnecessarily?
- Has any child Objective been broadened to absorb a review suggestion or an unrelated parent's outcome?
- Can the children all pass while the parent outcome is missed because necessary behavior has never been stated in any owning child's Requirements?
- Has an Objective been used to smuggle implementation obligations past the Requirements-only boundary?
- Has a necessary clarification been rejected solely because the short Objective does not enumerate that detail?

If yes, restructure before creating the Issues.

### 7a. Keep review and repair bounded by the Objective

Reviewers evaluate the contract against the fixed Objective; they do not optimize or rewrite the Objective. Responding agents evaluate findings rather than accepting every proposal.

Classify each proposed change before editing:

- **In-scope clarification or false-success prevention:** demonstrate the material ambiguity, contradiction, missing oracle, or plausible incorrect outcome, identify affected REQ IDs where applicable, and make the smallest sufficient contract correction.
- **Necessary independent prerequisite:** identify why the objective cannot safely be achieved without it, then reconsider the staged Issue graph; do not silently enlarge the current child.
- **Adjacent improvement:** exclude it from the current Issue's blockers and required completion scope.
- **Objective change or unresolved user choice:** stop automatic purpose-changing edits and ask the user. Do not publish a guessed replacement purpose and remove it afterward.

For an Objective-based gap, quote the relevant part of the fixed Objective, show the concrete observable mismatch, and distinguish the missing explicit Requirement from an implementation defect. A claim that something is "more robust", "best practice", "architecturally cleaner", or useful for hypothetical future support is not a blocker without a material in-scope consequence.

Existing material defects in explicit Requirements remain reviewable even when their details are not repeated in the Objective. Conversely, review history alone cannot make a currently coherent, in-scope contract defective. A self-consistent rewrite pursuing a different outcome is not made acceptable merely by copying that rewrite into the latest Objective field.

Stop specification review when no demonstrated material contract defect remains within the fixed scope. Do not search indefinitely for additional desirable features or require exhaustive formalization of unrelated systems. A later-discovered genuine material defect must still be reported; iteration count and a desire to finish are not correctness oracles.

When responding to review, preserve the Objective verbatim, address valid findings, and explicitly reject or separate out-of-scope suggestions. Keep Requirements and Acceptance Scenarios aligned without promoting the reviewer's prose into new authority.

### 7b. Perform a standalone decidability audit

For each child, temporarily hide its Context, Implementation Notes, review history, and the bodies of its parent and siblings. Using only its Requirements, determine the outcome of one normal case and one material boundary/conflict case for every row identified in 5a/5b. The Objective may identify a missing outcome, but it cannot supply an unstated implementation rule.

If the answer requires looking up a sibling's placement rules, assuming an authorization meaning, narrowing a universal statement from an example, or selecting an unspecified lifecycle/conflict policy, the contract is not yet independently decidable. Restore only the minimal necessary semantics in the owning child's Requirements, or record the unresolved decision/prerequisite. Do not copy entire sibling contracts. Record the demonstrated ambiguity and its owning REQ IDs rather than merely declaring that this audit passed.

### 7c. Perform a standalone implementability audit

Decidability and implementability are different audits. A child can have a perfectly self-consistent contract and still be too large because satisfying it requires inventing several independent architectural capabilities.

For each proposed child, temporarily consider only the current repository, that child's Requirements and its declared prerequisite outputs. For every Requirement identify:

- the supported production entry that reaches the behavior;
- the required runtime/semantic capability;
- the authoritative producer of its decisive fact;
- whether that producer already exists, is supplied by a declared prerequisite, or must be newly implemented here;
- the consumer of that fact;
- the explicit failure/unavailable/unknown state;
- the production-origin test oracle.

Then ask:

- Could a competent implementation agent complete this child by creating one bounded new capability or one bounded integration layer, or would it need to invent several independent architectures?
- Are there multiple newly required producers with independent state machines, privilege/control surfaces, cleanup semantics or test harnesses?
- Would one part remain independently useful and correct if another missing part were not yet implemented?
- Does the child require materially different OS/runtime authorities or external systems merely because they share an eventual success gate?
- Could the child be made green only by fabricating evidence, defaulting unknown to success, disabling all use, or replacing a real production oracle with a helper-level test?

If the answers reveal multiple independent new producers/capabilities, the child fails this audit and must be decomposed even when its Objective is short and its Requirements are decidable. Re-run sections 4a through 4c after splitting.

An integration child may remain after producer Issues are split out. It is independently implementable when its bounded responsibility is to compose already-defined prerequisite outputs at one authoritative production boundary, preserve same-operation/same-invocation causality, and verify the joined behavior. The integration child must not silently re-own the producers it depends on.

### 8. Test-oracle requirements

For each child, require tests from the supported production origin through the boundary where correctness can be lost.

Treat materially different test harnesses as decomposition evidence, not merely a testing inconvenience. If separate Requirements need different authoritative runtime boundaries because they exercise independent capabilities—for example process-lifetime supervision, filesystem write prevention and external publication delivery—re-run the capability split gate in 4a. One child may still have multiple tests for one mechanism; the signal is independent authorities, not test count.

Do not accept helper-only tests when the real risk is in orchestration.

Examples:

- production PR processing → durable claim → external `workflow_dispatch`;
- restart → authoritative reevaluation → duplicate suppression;
- webhook payload → durable invalidation queue;
- Issue/PR labels → production prompt rendering;
- PR recovery origin → next-attempt transition → redispatch eligibility.

Include adversarial scenarios for:
- crash/restart;
- ambiguous transport result;
- stale external evidence;
- missing external evidence;
- concurrent worker ordering;
- failure to persist authoritative state.

For Requirements whose correctness depends on concurrency, contention, or overlapping execution, do not accept a test that merely starts multiple workers/processes concurrently. The Acceptance Scenario must establish through an observable oracle that the material contested state or ordering was actually reached, then verify the required transition from that state. A test that can pass because scheduling happened to serialize the operations is insufficient.

When progress after contention is required, state whether progress must occur in the same participant/lifecycle incarnation or whether restart/reconstruction is permitted, and test that distinction explicitly. If the Requirement promises continued participation after contention resolves, a replacement participant succeeding is not evidence that the original participant recovered unless replacement is explicitly allowed by the contract.

For changes to authoring or review prompts, also verify the actual production prompt assembly and the behavior the prompt is intended to induce. Cover both a genuine missing invariant and a superficially sophisticated but out-of-scope demand. Include Objective deletion/replacement, purpose-preserving clarification, and parent/child scope leakage when relevant.

A test double returning a preselected READY/BLOCKED verdict proves orchestration or parsing, not that a model distinguishes drift from a legitimate correction. Keep deterministic boundary tests and semantic prompt-evaluation cases separate, and state which evidence each provides.

For operation variants and delayed authority, use the 5a/5b tables to select semantically different cases rather than enumerating a Cartesian product. Relevant cases include Copy versus Cut through the actual clipboard origin; project write denial despite an editable-looking surface; authorized read-only Copy versus denied mutation; valid page-root/Text/Layout placement versus a childless-leaf destination; and capture followed by an affected occurrence move/removal before execution. For history, pair a conflict with an unrelated peer source edit that must survive, exercise both Undo and Redo, and observe state, identities, consumption, refusal side effects, and history position as required by the chosen contract. Do not make these examples new feature obligations.

### 9. Issue authoring rules

For each non-trivial child Issue, put structural metadata before the semantic contract:

`Parent-Issue: #<parent>`

`Blocked-By:`

Use exactly one `Blocked-By:` line. Leave it empty for a dependency root; otherwise list every intended direct prerequisite sibling as comma-separated local references such as `Blocked-By: #123, #456`. Keep both structural metadata lines outside `## Requirements`.

Then use:

## Objective

One or two short sentences stating this Issue's fixed outcome. No list, implementation plan, or hidden Requirements. The Objective anchors specification scope and is immutable during automated review/repair; it is not an additional merge-blocking implementation contract.

## Context

Informative only.

## Requirements

Only machine-parseable entries:

`REQ-001: ...`

Requirements are the only merge-blocking implementation contract. Every non-empty normative entry must be a single `REQ-NNN: ...` line; do not put inner headings, code fences, explanatory paragraphs, or continuation lines in this section. Keep stable IDs.

State every implementation obligation explicitly here, including any essential outcome or preservation invariant identified through the Objective. Neither the Objective, parent prose, review comments, nor an Acceptance Scenario can supply an unstated merge-blocking Requirement.

## Acceptance Scenarios

Concrete Given / When / Then scenarios referencing Requirements.

## Non-goals

Explicitly exclude adjacent semantic layers.

## Implementation Notes

Optional, non-normative guidance only.

Add `difficult` only when the child itself contains intrinsically difficult concurrency, persistence, external-operation ambiguity, runtime enforcement or architectural reasoning **within one bounded correctness responsibility**. Difficulty caused by combining multiple absent capabilities is evidence for decomposition, not a reason to keep one broad Issue and label it `difficult`.

Do not add `@auto-coder`.

### 10. Before creating the Issues, report the proposed decomposition

Return:

- the discovered responsibilities;
- the capability inventory from 4a for every proposed child, including every `producer: none`;
- the authoritative producer-provenance audit from 4b for success/refusal/settlement/authorization predicates;
- the proposed Issue list and the exact one- or two-sentence Objective for each Issue;
- dependency/order graph, including the exact `Blocked-By:` value planned for every child;
- the mandatory split-gate result, including an explicit atomicity justification for any child that still owns two or more newly absent capabilities;
- the `CANNOT_FIX` pre-mortem result for each child and how any missing prerequisite was assigned;
- which Issues need `difficult`, after decomposition rather than instead of it;
- why each boundary is independently testable and how the children collectively cover the required outcome without hidden parent work;
- any materially different authoritative test harnesses and why they do or do not imply another split;
- the standalone implementability-audit result from 7c, including every new producer owned by the child versus supplied by prerequisites;
- any excluded adjacent improvements and any proposed change to a previously fixed Objective;
- the compact variant/transition coverage and standalone-decidability counterexamples where 5a/5b apply, including owning REQ IDs and any unresolved authority, lifecycle, or conflict-policy decisions;
- any unresolved decision that materially changes observable behavior.

If an unresolved decision is required for an implementable contract, stop and ask me.

Otherwise, create the tracking parent first, then create children in a topological order where every Issue named by a child's `Blocked-By:` already exists. Put the reported `Parent-Issue:` and final `Blocked-By:` metadata in each child's initial body at creation time; do not depend on Issue-number ordering or a later metadata repair. Preserve the reported Objectives verbatim in their bodies and do not silently rewrite them during creation or subsequent review response.

### 10a. Submit the created Issue set for implementation

Treat `implementation-ready` as the final submission signal, not as ordinary metadata to attach while an Issue is being created.

After Issue creation is fully complete:

- for a standalone Issue, add the `implementation-ready` label to that Issue;
- for a decomposed Issue family, create the tracking parent and every intended child first, establish all final `Parent-Issue:` and `Blocked-By:` metadata and any other required labels, then add `implementation-ready` to the tracking parent only;
- do not add `implementation-ready` to generated children as part of normal decomposition submission; the parent submission governs validation and sequential child eligibility;
- do not include `implementation-ready` in the initial Issue-creation request, because creation-time submission can expose an incomplete Issue or incomplete parent/child graph to Auto-Coder;
- if Issue creation, parent/child establishment, dependency metadata, or any required pre-submission mutation fails, do not add `implementation-ready`;
- if adding `implementation-ready` fails, report the failure and do not claim that the Issue or Issue family was submitted successfully.

For a decomposed family, the tracking parent's `implementation-ready` label is a submission of the completed specification set. It does not turn the coordination-only parent into an implementation target.

Adding `implementation-ready` must therefore be the last GitHub mutation performed by this preflight-created submission, except for recovery of a failed readiness-label operation itself.

---

## Special rule for retiring an old coordination mechanism

When the request is “remove/deprecate/retire X”, do **not** begin by deleting X.

First answer:

1. What correctness responsibilities does X currently perform?
2. Which of those responsibilities already have an internal authoritative replacement?
3. Which still require a replacement?
4. Which uses are merely presentation or stale compatibility?
5. Which self-generated events or external side effects does X create?
6. Which recovery paths rely on changing X as an implicit wake-up signal?
7. What unrelated helpers happen to live in the same module as X?

Only after answering these should Issues be created.

A retirement Issue should normally say:

> X is no longer authoritative.

It should not accidentally say:

> redefine the entire subsystem that X happened to touch.

---

## Expected result style

Write issue in english.

Prefer a structure such as:

- Parent: tracking/coordination only
  - Child 1: prerequisite correctness mechanism — `Blocked-By:`
  - Child 2: semantic non-interference / compatibility isolation — `Blocked-By: #<Child 1>`
  - Child 3: remove legacy lifecycle authority — `Blocked-By: #<Child 2>`
  - Child 4: optional architectural cleanup — `Blocked-By: #<required prerequisite(s)>`

The example is a chain only for illustration. Use the actual dependency DAG: independent roots should each have an empty `Blocked-By:`, independent eligible siblings may run concurrently, and a join child should list every direct prerequisite it truly requires.

Use fewer or more children when the code shows different correctness boundaries.

The decomposition must follow semantic responsibility, not file count. Interpret "semantic responsibility" concretely as a bounded independently implementable capability, policy authority or integration boundary with a named authoritative producer and test oracle. Do not let an aggregate label such as "execution boundary" hide process supervision, filesystem confinement, network/publication mediation, credential authority and evidence composition in one child merely because they contribute to one final outcome.

For every Issue, show its short Objective before its detailed contract. The parent coordinates completion; each child owns one bounded outcome. Review may improve the contract for that outcome, never silently change the outcome itself.
