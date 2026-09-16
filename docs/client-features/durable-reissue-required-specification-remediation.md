# Durable reissue-required specification remediation

Individual Issue specification review treats decision-critical semantics delegated
only to existing/current behavior, authorization rules, repository conventions, or
external authorities as an underspecified contract. It also verifies that actor,
permission/capability, lifecycle, and ownership distinctions that change required
behavior are established and reachable from the normative Requirements rather than
invented by acceptance scenarios or contextual prose. Repository implementation and
tests may expose the impact of a gap but cannot repair its normative authority.
References to existing fields, named interfaces, or standards remain valid when the
Requirements themselves define enough observable semantics to decide correctness.

Specification validation has a durable repair-round circuit breaker. Individual
Issues and parent decomposition sets independently count distinct authoritative
contract generations only when their trustworthy `BLOCKED` + `EDIT_IN_PLACE`
decision explicitly authorizes another automatic contract repair. Diagnostic
publication and readiness withdrawal never consume a round. The
`[validation].repair_round_limit` positive integer setting
defaults to `3`. Once exhausted, the next new generation is still reviewed. READY and ERROR retain
their semantic meanings, genuine `REISSUE_REQUIRED` remains terminal, and another
`EDIT_IN_PLACE` result durably pauses its repair episode without changing remediation
or writing a reissue marker. Generation-to-episode assignments are immutable, so
restart, policy changes, limit changes, duplicate observations, and exact reversion
cannot mint another allowance. A never-before-associated corrected generation may
start a fresh episode when the applicable standalone or parent submission is renewed,
or under uninterrupted inherited parent admission for a child.

Every enabled individual validation in a submitted family is a complete-family
prerequisite: one current child BLOCKED or paused result prevents every open sibling
from dispatch, including when the blocking child is retained closed in membership.
Durable semantic reissue markers remain unconditional terminal admission stops when
their validation category is disabled. Decomposition BLOCKED effects additionally
require the authoritative parent to remain open as well as currently submitted.

Individual and parent/decomposition specification results carry a strict
remediation disposition in addition to their unchanged semantic verdict.
`READY` and `ERROR` use `NONE`; `BLOCKED` uses either `EDIT_IN_PLACE` or
`REISSUE_REQUIRED`. A current `REISSUE_REQUIRED` decision is durably recorded
against the stable repository/Issue number before GitHub side effects. The stop
survives edits, relabeling, validation generations, and controller restarts, so
that Issue (or parent set) cannot later authorize implementation. Stale or
withdrawn submissions cannot create the marker. Child-level terminal results
mark only the child and withdraw only its own explicit readiness label,
preserving parent readiness and leaving the parent and siblings unmarked.

Each stable Issue number also receives an immutable individual-review baseline
when its first valid authoritative title, body, and Requirement manifest enter
semantic analysis. Later reviews receive that baseline and the deduplicated
history of material BLOCKED outcomes actually applied to current submissions.
This evidence is used only after the current-contract analyzer independently
returns `BLOCKED`: responsibility-boundary expansion, unsafe decomposition, or
cumulative material drift selects `REISSUE_REQUIRED`, while a correction within
the baseline responsibility selects `EDIT_IN_PLACE`. History, Issue size,
difficulty, and mere textual change cannot turn `READY` or `ERROR` into
`BLOCKED`, and child-level reissue remains scoped to the child.

The specification-review regression corpus includes a provenance-preserving,
explicitly synthetic reconstruction of the historical `auto-coder#1790`
sequence. It records each supporting validation/supersession comment and treats
the sequence only as a negative remediation oracle: cumulative independent
ownership, urgency, label-resolution, prompt-selection, and logical-owner layers
must not be mistaken for endlessly local edits. The reconstructed prose is not
an exact GitHub snapshot and is not a normative source for production behavior.

Each stable parent Issue number likewise receives an immutable decomposition
baseline when its first valid authoritative parent and complete direct-child set
enter semantic analysis. The baseline preserves every stable Issue number,
title, body, and Requirement manifest exactly; later edits, membership changes,
readiness changes, and validation-policy changes do not rewrite it. Applied
material set-review outcomes are retained and supplied as non-normative evidence.
After the current-set analyzer independently returns `BLOCKED`, the reviewer
selects `REISSUE_REQUIRED` when the smallest safe remedy replaces the
parent/direct-child responsibility graph or accumulated applied repairs have
materially transformed it. Otherwise a same-graph correction remains
`EDIT_IN_PLACE`. Historical drift and simple size or membership changes cannot
create a blocker, change current Requirements, or alter a current `READY` /
`NONE` result. Set reissue marks only the stable parent; children remain subject
to their independent review lifecycles.
