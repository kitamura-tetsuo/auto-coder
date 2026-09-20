# Auto-Coder: as-is execution sequences

These diagrams are static, explanatory source traces, not a proposed architecture, a runtime recording, or a new behavioral specification. Each document names its inspected source commit; later commits can behave differently. The original E/C/R/S/M overview is pinned to **`95ff379d2bd69e54a53f14c208eedaca5a58e3df`**. The detailed Issue implementation series (I/L/P/T/O) is pinned to **`d704b54e59ea559cce17d5cfe1a0841ac3ef465d`** and does not silently refresh the older diagrams.

The scope is the controller's Issue/PR lifecycle: startup, candidate intake, specification admission, implementation ownership, CI, ordinary and two-tier review, merge delivery, and restart/retry entrypoints. Backend SDK internals, every CLI command, and every configuration combination are not expanded. A call to a provider helper is not presented as proof that the remote operation succeeded.

## Reading order

| Document | Sequences |
| --- | --- |
| [Engine and Issue admission](engine-and-issues.md) | E1 startup; E2 invalidation worker; E3 specification/family admission; E4 ownership and dispatch |
| [PR intake, CI and repair](pr-ci-and-repair.md) | C1 PR intake and CI routing; C2 manual CI dispatch; C3 failure repair routing |
| [Ordinary adversarial validation](pr-adversarial-validation.md) | R1 eligibility and reuse; R2 execution and acceptance; R3 publication and rollback |
| [Two-tier PR review](pr-two-tier-review.md) | S1 strong audit and publication; S2 ordinary closure of strong findings |
| [Merge and recovery](pr-merge-and-recovery.md) | M1 final merge gates; M2 durable approval/merge effects; M3 scheduler-driven resumption |

### Detailed Issue implementation lifecycle

This series expands the Issue side beyond the original E3/E4 overview. Its entry document defines the distinct generation, retry, provider and retirement identities.

| Document | Sequences |
| --- | --- |
| [Issue implementation lifecycle](issue-implementation.md) | I1 submission/specification; I2 ownership and reserved dispatch |
| [Local execution](issue-local-execution.md) | L1 branch/edit/commit; L2 PR publication and association |
| [Provider dispatch](issue-provider-dispatch.md) | P1 quota/routing; P2 Codex claim; P3 singleton Jules/Claude; P4 speculative Jules submission |
| [Retry and recovery](issue-retry-and-recovery.md) | T1 explicit retry; T2 same-request replay; T3 stale Jules replacement; T4 initial Codex PR recovery |
| [Ownership and resumption](issue-ownership-and-resumption.md) | O1 launcher return; O2 terminal PR-backed reclamation; O3 deferred Issue reevaluation |

Each sequence has a scope/precondition and pinned source links. Separate diagrams are joined only at their named continuation points; they are not independent services or newly introduced event handlers. Early-return branches terminate the illustrated invocation, not necessarily the Issue/PR lifecycle.

## Notation and identities

Participants name responsibilities and identify their actual implementation in the accompanying source references. A store participant is a separate persistence boundary, not evidence that it shares a transaction with another store. Solid arrows denote calls/requests; dashed arrows denote returns/observations. They do not imply network delivery guarantees. Notes marked `C1`, `C2`, etc. identify hypothetical interruption positions in actual code ordering, not tested crash outcomes.

| Symbol | Meaning in these diagrams |
| --- | --- |
| `N`, `I` | PR number and Issue number in one repository |
| `H`, `B` | Examined PR head SHA and base SHA; a newer head is `H2` |
| `g` | Durable **invalidation** claim generation, not the Issue specification identity |
| `V1`, `V2` | Ordinary validation attempts ordered by allocated attempt sequence, not completion time |
| `review_id` | Audit identity of one logical reviewer execution; distinct from an attempt and GitHub review ID |
| `M`, `P` | Two-tier contract snapshot identity and strong reviewer policy identity |
| `owner`, `execution_id` | Logical implementation ownership and one local execution within it; returning from execution does not prove remote work ended |

The engine, PR processor, and specialized stores do not all use one universal lifecycle key. In particular, manual CI dispatch keys include `H` and workflow; the merge-operation key is API origin + repository + PR, with expected head stored in the operation. See the corresponding diagrams before comparing or combining their state. The [Issue implementation identity table](issue-implementation.md) additionally distinguishes specification identity `V`, implementation generation `G`, retry request/attempt `R/A`, numeric attempt `a`, and reservation incarnation/activity revision.

## Important source-reading findings

The daemon at the original snapshot already has a durable invalidation loop and separate Issue/PR workers. Its producer loop is maintenance, not a repeated general candidate scan. Specialized recovery schedulers coexist with this path; the diagrams do not replace them with a hypothetical single event bus. [Source][engine]

`LabelManager` is a side-effect-free scope. Some caller comments still describe adding/removing the historical `@auto-coder` label, but its actual methods do not perform those mutations. No label-lock arrows are inferred from those comments. [Source][labels]

The original snapshot contains production strong-audit execution and two-tier publication consumption. It also returns from an unsuccessful green-CI merge route rather than treating that result as evidence of CI failure. Earlier descriptions of these missing connections do not describe this snapshot. [Source][strong-and-route]

There are deliberate limits to what a static trace establishes. For example, a persisted CI claim can suppress a request even when the process stopped before sending it; an accepted review can still await publication; and a successful local launch does not establish completion of a provider task. Those distinctions are retained rather than replaced with idealized recovery guarantees.

The detailed Issue series also distinguishes action strings from actual GitHub mutations, provider-specific send/receipt ordering, and execution completion from capacity retirement. Its [local](issue-local-execution.md), [provider](issue-provider-dispatch.md), and [ownership](issue-ownership-and-resumption.md) documents identify the corresponding source call sites rather than treating a success-looking message as an effect receipt.

## Relationship to existing documentation

The source permalinks establish what was inspected for each snapshot. Existing feature fragments under `docs/client-features/` retain their own role; this directory neither aggregates nor replaces them. These sequences are informative and do not introduce merge-blocking Requirements or endorse existing behavior as the desired design.

To refresh a diagram, choose a new source commit, retrace both the caller and the effect-owning helper, and update the snapshot and its source links together. Do not silently add an intended repair to an as-is arrow.

[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L2467-L2878
[labels]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/label_manager.py#L340-L393
[strong-and-route]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L4110-L4470
