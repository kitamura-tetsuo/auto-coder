# Operator-controlled management policy

`auto_coder.operating_policy` is an importable, side-effect-free decision producer
for management consumers. It loads a fresh operator-owned local YAML file for each
evaluation and returns a typed `PolicyDecision`; it does not require an MCP server,
a worker, Docker, the network, or GitHub credentials. Authentication and scopes,
operation-specific correctness checks, durable operation ownership, and execution
remain independent consumer responsibilities.

## Configuration

The runnable files `examples/operating-policy-observe.yaml`,
`examples/operating-policy-beta.yaml`, and `examples/operating-policy-full.yaml`
show the complete format. `profile` is exactly `observe`, `beta`, or `full`; omitting
it means `observe`. `targets` is a non-empty mapping from a logical target ID to its
authoritative `channel` (`beta` or `release`) and a non-empty list of permitted
`owner/name` repository identities. Unknown fields, profiles, channels, malformed
documents, and unreadable explicitly selected files fail closed.

Only a trusted local operator changes this file. There is intentionally no request,
model-output, CI-result, acceptance, approval, or transition API that changes the
profile or mapping, and there is no automatic escalation. A consumer calls
`evaluate_operating_policy(path, intent)` at admission and again immediately before
its first forward effect. Each call reloads the path. An old `ALLOWED` object is a
record of a decision, never reusable authority.

## Typed contract and matrix

`PolicyIntent` identifies a logical target, repository, closed `ActionClass`, optional
caller channel claim, and whether the request is a forward operation or
reconciliation. `PolicyDecision` always reports `ALLOWED`, `DENIED`, or `UNAVAILABLE`,
a stable `ReasonCode`, the evaluated profile and authoritative target facts when
known, and a revision. Valid snapshots use a canonical `sha256:` content revision;
malformed readable content uses `invalid-sha256:`, while an unreadable path reports
the explicit revision `unavailable`.

| Profile | Read-only inspection | Beta worker/deployment | Release worker/deployment | Release promotion |
| --- | --- | --- | --- | --- |
| `observe` | ALLOWED | DENIED | DENIED | DENIED |
| `beta` | ALLOWED | ALLOWED | DENIED | DENIED |
| `full` | ALLOWED | ALLOWED | ALLOWED | ALLOWED for release targets only |

Every row still requires a configured target. A supplied repository must match its
target, every mutation must supply a matching repository, and a caller channel claim
must agree with the mapping. Unknown actions, targets, and operation classifications
are denied. Inspection is only a decision: the module cannot create or claim a
worker, mutate a tag, select a profile, or execute management effects. Missing target
facts remain missing in the result rather than being synthesized.

`reconciliation` is separately represented but does not grant a bypass: mutations
classified that way are denied with `reconciliation_not_authorized`. A consumer may
perform exact bounded recovery only under its own contract for an already admitted
operation and durable pre-effect facts. Labels such as `rollback`, generic approval
records, or a proposed new candidate do not establish that authority.

## Rollout checklist

Before deliberately changing to `beta` or `full`, the operator should:

1. Record the current canonical revision, target mapping, and observed decision
   evidence for each intended action.
2. Confirm independently that authentication/scopes and each operation's correctness
   gates are enforced by the integrating consumer.
3. Confirm target repositories and channels against deployment ownership records.
4. Change the local file deliberately, record the new revision, and exercise a fresh
   pre-effect evaluation; retain the evidence needed to return to `observe`.

This policy constrains only management paths that explicitly integrate it. It does
not restrict existing direct operator CLI use, arbitrary GitHub clients, or hostile
code that bypasses the supported path, and it does not change the normal worker
Issue/PR/review/merge loop.
