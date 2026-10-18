# Repository Issue runtime composition

`IssueRuntimeComposition` is the repository-bound assembly point for Issue
startup and recovery. It registers the existing pending-work stage handlers,
connects accepted Codex retry reconciliation, and creates the existing durable
local Issue runner. The component owns no durable queue or state transition;
specialized PR, merge, publication, invalidation, and review services retain
their existing authorities.

Startup first binds the repository's usable pending-work store and all stage
handlers. It then rediscovers unfinished accepted Codex receipts by their
original request and Issue identities and starts pending-work recovery. The
engine's authoritative GitHub enumeration and local-result finalization remain
behind the existing startup reconciliation barrier. Only after that barrier
succeeds does the composition start the local Issue executor and the engine
admit ordinary producer and worker traffic.

Each composition can register and start its recovery and local executor only
once. Both executors are repository-filtered, retained due times remain owned
by their durable stores, and shutdown continues to use the engine's shared
shutdown event and invocation-admission drain. Registration and rediscovery do
not prove an external effect completed and cannot submit replacement provider
work.
